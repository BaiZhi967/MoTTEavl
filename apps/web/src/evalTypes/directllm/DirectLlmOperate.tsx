import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { UploadSimpleIcon } from "@phosphor-icons/react";
import {
  createDirectLlmRun, getDirectLlmBuiltins, getDirectLlmOverview, getModels, importDirectLlm,
  type DirectLlmBuiltin, type DirectLlmPreset, type ModelRecord,
} from "../../api/client";
import { ModelPicker } from "../../components/ModelPicker";
import { suiteRoutes } from "../registry";
import { clearCaseSelection, loadCaseSelection, randomSeed, selectionRequest,
  type StoredSelection } from "./selection";
import { SCORER_OPTIONS, datasetScorer, scorerLabel, sortPresets } from "./presets";

const ROUTES = suiteRoutes("direct-llm");
type CaseMode = "all" | "ids" | "random";
/** 数据集缺失时的兜底展示值；真实预设由数据集记录声明（preset.eval.max_output_tokens）。 */
const FALLBACK_BUDGET = 1024;

export function DirectLlmOperate() {
  const navigate = useNavigate();
  const [overview, setOverview] = useState<DirectLlmPreset[]>([]);
  const [chosenScenario, setChosenScenario] = useState("");
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [builtins, setBuiltins] = useState<DirectLlmBuiltin[]>([]);
  const [chosenBuiltin, setChosenBuiltin] = useState("");
  const [builtinVersion, setBuiltinVersion] = useState("");
  const [content, setContent] = useState("");
  const [name, setName] = useState("");
  const [version, setVersion] = useState("");
  const [license, setLicense] = useState("internal-sample");
  const [scorer, setScorer] = useState("");
  const [source, setSource] = useState("");
  const [importing, setImporting] = useState(false);
  const [running, setRunning] = useState(false);
  const [failures, setFailures] = useState<{ model: string; error: string }[]>([]);
  const [launched, setLaunched] = useState<string[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [caseMode, setCaseMode] = useState<CaseMode>("all");
  const [randomCount, setRandomCount] = useState("10");
  const [seed, setSeed] = useState(randomSeed);
  const [picked, setPicked] = useState<StoredSelection | null>(null);
  const [levels, setLevels] = useState<Record<string, string>>({});
  const [temperature, setTemperature] = useState("");
  const [budget, setBudget] = useState("");
  const [fileName, setFileName] = useState("");
  const filePicker = useRef<HTMLInputElement>(null);

  const presets = useMemo(() => sortPresets(overview), [overview]);
  /* 默认选中题数最多的数据集；用户一旦明确选择就不再被覆盖。 */
  const preset: DirectLlmPreset | undefined =
    presets.find((item) => item.scenario === chosenScenario) ?? presets[0];
  const usableBuiltin = builtins.find((item) => item.id === chosenBuiltin && item.importable);
  /* 指定题目来自「题目」页：数据集对不上时不能直接发起，避免把别的题集 id 发上去。 */
  const pickedMismatch = Boolean(picked && preset && picked.dataset !== preset.dataset);
  const usablePicked = picked && !pickedMismatch ? picked : null;
  const reasoningModels = useMemo(
    () => models.filter((model) => selected.includes(model.id) && model.reasoning?.supported),
    [models, selected]);
  const runSize = caseMode === "random" ? Math.max(0, Math.floor(Number(randomCount) || 0))
    : caseMode === "ids" ? usablePicked?.caseIds.length ?? 0
      : preset?.cases ?? 0;
  /* 数据集预设的输出上限（不填就是它，填了就以运行快照为准），展示与服务端同源。 */
  const presetBudget = typeof preset?.eval?.max_output_tokens === "number"
    ? preset.eval.max_output_tokens : FALLBACK_BUDGET;
  const effectiveBudget = budget.trim() ? Number(budget) : presetBudget;

  const refresh = useCallback(async () => {
    try {
      const payload = await getDirectLlmOverview();
      setOverview(payload.items);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    getModels().then((payload) => setModels(payload.items)).catch(() => undefined);
    /* 默认选中第一个可导入的样例：选择值必须落进 state，不能只靠 select 的显示回退，
       否则「导入」按钮拿不到 id（表现为按钮可用但点了没反应）。 */
    getDirectLlmBuiltins()
      .then((payload) => {
        setBuiltins(payload.items);
        setChosenBuiltin((current) => current
          || payload.items.find((item) => item.importable)?.id
          || payload.items[0]?.id
          || "");
      })
      .catch(() => undefined);
    setPicked(loadCaseSelection());
  }, [refresh]);

  /* 数据集切换后旧勾选不再适用；换数据集时清空，避免「指定题目」带着别的题集 id 发起。 */
  useEffect(() => {
    if (picked && preset && picked.dataset !== preset.dataset) {
      setCaseMode((mode) => (mode === "ids" ? "all" : mode));
    }
  }, [picked, preset]);

  const applyReceipt = (receipt: { imported: string; scenario: string; scorer: string; cases: number }) => {
    setMessage(`已导入 ${receipt.imported} · ${scorerLabel(receipt.scorer)} · ${receipt.cases} 题`);
    setChosenScenario(receipt.scenario);
  };

  const importBuiltin = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    if (!usableBuiltin) return;
    setImporting(true);
    setError("");
    setMessage("");
    try {
      // 数据集名与评分器默认取内置注册表；这里只把 id 交给服务端。
      applyReceipt(await importDirectLlm({
        builtin: usableBuiltin.id,
        version: builtinVersion.trim(),
      }));
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setImporting(false);
    }
  };

  const importContent = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setImporting(true);
    setError("");
    setMessage("");
    try {
      applyReceipt(await importDirectLlm({
        content,
        name: name.trim(),
        version: version.trim(),
        license: license.trim(),
        scorer: scorer || undefined,
        source: source.trim() || undefined,
      }));
      setContent("");
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setImporting(false);
    }
  };

  const readFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      setContent(await file.text());
      setFileName(file.name);
      if (!name.trim()) setName(file.name.replace(/\.(jsonl|json|txt)$/i, ""));
      setError("");
    } catch (e) {
      setError(`读取本地文件失败：${String(e)}`);
    } finally {
      // 允许连续选择同一个文件（否则 change 不会再次触发）
      event.target.value = "";
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
      /* 非数字输入不进 manifest（服务端只认数字参数，NaN 会被写成 null 再被静默丢弃）。 */
      const parameters: Record<string, number> = {};
      if (Number.isFinite(Number(temperature)) && temperature.trim()) {
        parameters.temperature = Number(temperature);
      }
      if (Number.isFinite(Number(budget)) && budget.trim()) {
        parameters.max_output_tokens = Number(budget);
      }
      for (const model of selected) {
        try {
          const run = await createDirectLlmRun({
            model,
            scenario: preset.scenario,
            case_selection: selection,
            ...(Object.keys(parameters).length > 0 ? { parameters } : {}),
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
      if (failed.length === 0) setError("请先选择至少一个模型");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="page">
      <section className="panel detail" aria-label="Direct LLM 操作页">
        <div className="panel-head"><h2>Direct LLM 评测 · 操作</h2></div>
        {error && <p className="error">{error}</p>}

        <div className="operate-grid">
          <div className="operate-card">
            <h3 className="embed-title">数据集（pinned）</h3>
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
                          {item.scenario} · {item.cases} 题
                        </option>
                      ))}
                    </select>
                  </div>
                )}
                <p className="mono">{preset.scenario}</p>
                <p className="hint mono">
                  {preset.dataset} · {preset.cases} 题 · {scorerLabel(datasetScorer(preset))}
                </p>
                <p className="hint mono">
                  来源 {String(preset.provenance?.source ?? "—")}
                  {preset.eval?.prompt_version ? ` · prompt ${preset.eval.prompt_version}` : ""}
                  {preset.eval?.scorer_version ? ` · scorer ${preset.eval.scorer_version}` : ""}
                </p>
              </>
            ) : (
              <p className="hint">尚未导入数据集。用「内置样例」一键导入，或粘贴 / 选择本地 JSONL。</p>
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
          </div>

          <div className="operate-card">
            <h3 className="embed-title">跑测参数</h3>
            <label>
              temperature
              <input
                type="number" step="0.1" min="0" value={temperature}
                onChange={(change) => setTemperature(change.target.value)}
                placeholder="留空 = 模型档案默认值"
              />
            </label>
            <label>
              max_output_tokens
              <input
                type="number" min={1} value={budget}
                onChange={(change) => setBudget(change.target.value)}
                placeholder={`留空 = 数据集预设 ${presetBudget}`}
              />
            </label>
            <dl className="kv">
              <dt>题数</dt>
              <dd className="mono">
                {caseMode === "random" ? `${randomCount || 0} / ${preset?.cases ?? 0}`
                  : caseMode === "ids" ? `${usablePicked?.caseIds.length ?? 0} / ${preset?.cases ?? 0}`
                    : preset?.cases ?? "—"}
              </dd>
              <dt>输出上限</dt>
              <dd className="mono">{Number.isFinite(effectiveBudget) ? effectiveBudget : "—"}</dd>
              <dt>重试</dt><dd className="mono">0</dd>
            </dl>
            <p className="hint">
              输出上限不得超模型档案声明的上限；评分只看输出正文，流式思考会占满预算。
            </p>
          </div>

          <div className="operate-card">
            <h3 className="embed-title">内置样例</h3>
            <p className="hint">
              仓库自带的小型样例集（见 datasets/direct-llm/），用来在零外部数据的前提下跑通链路。
            </p>
            {builtins.length > 0 ? (
              <form onSubmit={importBuiltin} aria-label="导入内置样例">
                <label>
                  样例数据集
                  <select
                    className="control"
                    aria-label="内置样例"
                    value={chosenBuiltin}
                    onChange={(change) => setChosenBuiltin(change.target.value)}
                  >
                    {builtins.map((item) => (
                      <option key={item.id} value={item.id} disabled={!item.importable}>
                        {item.id} · {scorerLabel(item.scorer)} · {item.cases ?? "?"} 题
                        {item.importable ? "" : "（不可导入）"}
                      </option>
                    ))}
                  </select>
                </label>
                <p className="hint">{usableBuiltin?.description ?? "该样例不可导入。"}</p>
                <label>
                  数据集版本
                  <input
                    value={builtinVersion}
                    onChange={(change) => setBuiltinVersion(change.target.value)}
                    placeholder="留空 = 自动（同内容复用，否则下一个空号）"
                  />
                </label>
                <button type="submit" disabled={importing || !usableBuiltin}>
                  {importing ? "导入中…" : "导入所选样例"}
                </button>
              </form>
            ) : (
              <p className="hint">内置样例清单不可用（服务端样例目录缺失）。</p>
            )}
          </div>

          <div className="operate-card">
            <h3 className="embed-title">导入本地 JSONL</h3>
            <p className="hint">
              每行一个对象：<span className="mono">{'{"input": "题面", "expected": "期望", "scorer": "contains"}'}</span>。
              评分器可单题覆盖；没有 expected 的题记为「无判定」，不进分母。整份文件逐行校验，任何一行
              不合法都会整体拒绝。
            </p>
            <form onSubmit={importContent} aria-label="导入本地 JSONL">
              <div className="inline-field">
                <button type="button" onClick={() => filePicker.current?.click()}>
                  选择 .jsonl 文件
                </button>
                {fileName && <span className="hint mono">{fileName}</span>}
                <input
                  ref={filePicker}
                  type="file"
                  hidden
                  accept=".jsonl,.json,.txt"
                  onChange={(change) => void readFile(change)}
                />
              </div>
              <label>
                JSONL 内容
                <textarea
                  className="mono"
                  rows={6}
                  value={content}
                  onChange={(change) => setContent(change.target.value)}
                  placeholder={'{"input": "1+1=?", "expected": "2"}'}
                />
              </label>
              <details className="disclosure">
                <summary>高级设置</summary>
                <label>
                  数据集名
                  <input
                    value={name}
                    onChange={(change) => setName(change.target.value)}
                    placeholder="留空 = direct-llm-custom"
                  />
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
                  默认评分器
                  <select
                    className="control"
                    value={scorer}
                    onChange={(change) => setScorer(change.target.value)}
                  >
                    <option value="">exact（默认）</option>
                    {SCORER_OPTIONS.filter((option) => option.value !== "exact").map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  License
                  <input value={license} onChange={(change) => setLicense(change.target.value)} />
                </label>
                <label>
                  来源标记
                  <input
                    value={source}
                    onChange={(change) => setSource(change.target.value)}
                    placeholder="留空 = local-jsonl"
                  />
                </label>
              </details>
              <button type="submit" disabled={importing || !content.trim()}>
                <UploadSimpleIcon size={14} weight="bold" aria-hidden />
                {importing ? "导入中…" : "导入 JSONL"}
              </button>
              {message && <p className="import-feedback pass">{message}</p>}
            </form>
          </div>
        </div>

        <div className="actions">
          <button
            type="button"
            onClick={() => void doRun()}
            disabled={running || selected.length === 0 || !preset || runSize <= 0}
          >
            {running ? "创建中…" : `发起评测（${selected.length} 个模型 × ${runSize} 题）`}
          </button>
          <span className="hint">真实调用 · 产生费用 · 发起后自动进入过程页</span>
        </div>
        {failures.length > 0 && (
          <ul>
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
