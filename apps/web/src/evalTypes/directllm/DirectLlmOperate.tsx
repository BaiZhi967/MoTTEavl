import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Board } from "../../board/Board";
import { FieldGrid, Field, IssueBar } from "../../board/FieldGrid";
import { Link, useNavigate } from "react-router-dom";
import { CalculatorIcon, UploadSimpleIcon } from "@phosphor-icons/react";
import {
  createDirectLlmRun, dryRunDirectLlm, getDirectLlmBuiltins, getDirectLlmOverview,
  getDirectLlmSources, getModels, getSourceDetail, importDirectLlm,
  type DirectLlmBuiltin, type DirectLlmCaseSelection, type DirectLlmDryRunResponse,
  type DirectLlmPreset, type DirectLlmRunRequest, type DirectLlmSourceDetail,
  type DirectLlmSourceSummary, type ModelRecord,
} from "../../api/client";
import { ModelPicker } from "../../components/ModelPicker";
import { suiteRoutes } from "../registry";
import { clearCaseSelection, loadCaseSelection, randomSeed, selectionRequest,
  type StoredSelection } from "./selection";
import { SCORER_OPTIONS, datasetScorer, scorerLabel, sortPresets } from "./presets";

const ROUTES = suiteRoutes("direct-llm");
type CaseMode = "all" | "ids" | "random" | "profile";
type SourceTone = "success" | "error" | "info" | "warning" | "neutral";

const SOURCE_STATUS_META: Record<string, { label: string; tone: SourceTone }> = {
  approved: { label: "已批准", tone: "success" },
  "approved-internal": { label: "内部使用", tone: "info" },
  internal: { label: "内部使用", tone: "info" },
  restricted: { label: "受限", tone: "error" },
  pending: { label: "待审核", tone: "warning" },
};

const DISTRIBUTION_LABELS: Record<string, string> = {
  public: "公开",
  "internal-only": "仅内部",
  restricted: "受限",
  blocked: "已阻断",
};

const COMPARABILITY_LABELS: Record<string, string> = {
  established: "已建立",
  "not-established": "未建立",
  "not-applicable": "不适用",
};

/** 数据集缺失时的兜底展示值；真实预设由数据集记录声明（preset.eval.max_output_tokens）。 */
const FALLBACK_BUDGET = 1024;

export function DirectLlmOperate() {
  const navigate = useNavigate();
  const [overview, setOverview] = useState<DirectLlmPreset[]>([]);
  const [chosenScenario, setChosenScenario] = useState("");
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [builtins, setBuiltins] = useState<DirectLlmBuiltin[]>([]);
  const [managedSources, setManagedSources] = useState<DirectLlmSourceSummary[] | null>(null);
  const [sourceError, setSourceError] = useState("");
  const [showRestrictedSources, setShowRestrictedSources] = useState(false);
  const [openSourceId, setOpenSourceId] = useState<string | null>(null);
  const [sourceDetail, setSourceDetail] = useState<DirectLlmSourceDetail | null>(null);
  const [sourceDetailLoading, setSourceDetailLoading] = useState(false);
  const [sourceDetailError, setSourceDetailError] = useState("");
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
  const [profileName, setProfileName] = useState("");
  const [randomCount, setRandomCount] = useState("10");
  const [seed, setSeed] = useState(randomSeed);
  const [picked, setPicked] = useState<StoredSelection | null>(null);
  const [levels, setLevels] = useState<Record<string, string>>({});
  const [temperature, setTemperature] = useState("");
  const [budget, setBudget] = useState("");
  const [dryRun, setDryRun] = useState<DirectLlmDryRunResponse | null>(null);
  const [dryRunError, setDryRunError] = useState("");
  const [estimating, setEstimating] = useState(false);
  const [fileName, setFileName] = useState("");
  const filePicker = useRef<HTMLInputElement>(null);
  const selectionRestoreHandled = useRef(false);
  const sourceDetailRequest = useRef(0);
  const dryRunRequest = useRef(0);

  const presets = useMemo(() => sortPresets(overview), [overview]);
  /* 默认选中题数最多的数据集；用户一旦明确选择就不再被覆盖。 */
  const preset: DirectLlmPreset | undefined =
    presets.find((item) => item.scenario === chosenScenario) ?? presets[0];
  const profiles = preset?.contract_version === 2 ? preset.profiles ?? [] : [];
  const activeProfile = profiles.find((profile) => profile.name === profileName) ?? profiles[0];
  const visibleSources = managedSources?.filter((item) => showRestrictedSources || item.status !== "restricted") ?? null;
  const usableBuiltin = builtins.find((item) => item.id === chosenBuiltin && item.importable);
  /* 指定题目来自「题目」页：数据集对不上时不能直接发起，避免把别的题集 id 发上去。 */
  const pickedPreset = picked ? presets.find((item) => item.dataset === picked.dataset) : undefined;
  const pickedMismatch = Boolean(picked && preset && picked.dataset !== preset.dataset);
  const pickedUnavailable = Boolean(picked && presets.length > 0 && !pickedPreset);
  const usablePicked = picked && !pickedMismatch ? picked : null;
  const reasoningModels = useMemo(
    () => models.filter((model) => selected.includes(model.id) && model.reasoning?.supported),
    [models, selected]);
  const runSize = caseMode === "random" ? Math.max(0, Math.floor(Number(randomCount) || 0))
    : caseMode === "ids" ? usablePicked?.caseIds.length ?? 0
      : caseMode === "profile" ? activeProfile?.count ?? 0
        : preset?.cases ?? 0;
  /* 数据集预设的输出上限（不填就是它，填了就以运行快照为准），展示与服务端同源。 */
  const presetBudget = typeof preset?.eval?.max_output_tokens === "number"
    ? preset.eval.max_output_tokens : FALLBACK_BUDGET;
  const effectiveBudget = budget.trim() ? Number(budget) : presetBudget;
  const dryRunIdentity = JSON.stringify({
    scenario: preset?.scenario ?? null,
    models: selected,
    caseMode,
    profile: activeProfile?.name ?? null,
    randomCount,
    seed,
    caseIds: usablePicked?.caseIds ?? [],
    temperature,
    budget,
    levels,
  });

  useEffect(() => {
    dryRunRequest.current += 1;
    setDryRun(null);
    setDryRunError("");
    setEstimating(false);
  }, [dryRunIdentity]);

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
    getDirectLlmSources()
      .then((payload) => {
        setManagedSources(payload.items);
        setSourceError("");
      })
      .catch((e) => {
        setManagedSources([]);
        setSourceError(String(e));
      });
    setPicked(loadCaseSelection());
  }, [refresh]);

  /* 题目页返回时只恢复一次：优先切到勾选所属数据集，并显式进入指定题目模式。 */
  useEffect(() => {
    if (selectionRestoreHandled.current || presets.length === 0) return;
    selectionRestoreHandled.current = true;
    if (!picked) return;
    setCaseMode("ids");
    if (pickedPreset) setChosenScenario(pickedPreset.scenario);
  }, [picked, pickedPreset, presets.length]);

  const toggleSourceDetail = (sourceId: string) => {
    if (openSourceId === sourceId) {
      sourceDetailRequest.current += 1;
      setOpenSourceId(null);
      setSourceDetail(null);
      setSourceDetailError("");
      setSourceDetailLoading(false);
      return;
    }
    const requestId = sourceDetailRequest.current + 1;
    sourceDetailRequest.current = requestId;
    setOpenSourceId(sourceId);
    setSourceDetail(null);
    setSourceDetailError("");
    setSourceDetailLoading(true);
    getSourceDetail(sourceId)
      .then((detail) => {
        if (sourceDetailRequest.current === requestId) setSourceDetail(detail);
      })
      .catch((e) => {
        if (sourceDetailRequest.current === requestId) setSourceDetailError(String(e));
      })
      .finally(() => {
        if (sourceDetailRequest.current === requestId) setSourceDetailLoading(false);
      });
  };

  const applyReceipt = (receipt: { imported: string; scenario: string; scorer: string; cases: number }) => {
    setMessage(`已导入 ${receipt.imported} · ${scorerLabel(receipt.scorer)} · ${receipt.cases} 题`);
    setChosenScenario(receipt.scenario);
    setCaseMode("all");
    setProfileName("");
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
      const trimmedName = name.trim();
      const trimmedVersion = version.trim();
      const trimmedLicense = license.trim();
      const trimmedSource = source.trim();
      applyReceipt(await importDirectLlm({
        content,
        ...(trimmedName ? { name: trimmedName } : {}),
        ...(trimmedVersion ? { version: trimmedVersion } : {}),
        ...(trimmedLicense ? { license: trimmedLicense } : {}),
        ...(scorer ? { scorer } : {}),
        ...(trimmedSource ? { source: trimmedSource } : {}),
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

  const buildCaseSelection = (): DirectLlmCaseSelection | null => {
    if (caseMode === "profile") {
      return activeProfile ? { mode: "profile", profile: activeProfile.name } : null;
    }
    return selectionRequest(caseMode, {
      caseIds: usablePicked?.caseIds,
      count: Number(randomCount),
      seed,
    }) as DirectLlmCaseSelection | null;
  };

  const buildRunRequest = (model: string, selection: DirectLlmCaseSelection): DirectLlmRunRequest => {
    const parameters: Record<string, number> = {};
    if (Number.isFinite(Number(temperature)) && temperature.trim()) {
      parameters.temperature = Number(temperature);
    }
    if (Number.isFinite(Number(budget)) && budget.trim()) {
      parameters.max_output_tokens = Number(budget);
    }
    return {
      model,
      scenario: preset?.scenario,
      case_selection: selection,
      ...(Object.keys(parameters).length > 0 ? { parameters } : {}),
      ...(levels[model] ? { reasoning_level: levels[model] } : {}),
    };
  };

  const estimateRun = async () => {
    const selection = buildCaseSelection();
    if (!preset || selected.length !== 1 || !selection) return;
    const requestId = dryRunRequest.current + 1;
    dryRunRequest.current = requestId;
    setEstimating(true);
    setDryRun(null);
    setDryRunError("");
    try {
      const result = await dryRunDirectLlm(buildRunRequest(selected[0], selection));
      if (dryRunRequest.current === requestId) setDryRun(result);
    } catch (e) {
      if (dryRunRequest.current === requestId) setDryRunError(String(e));
    } finally {
      if (dryRunRequest.current === requestId) setEstimating(false);
    }
  };

  const doRun = async () => {
    if (!preset) return;
    const selection = buildCaseSelection();
    if (!selection) {
      setError(caseMode === "ids" ? "请先在「题目」页勾选要跑的题目"
        : caseMode === "profile" ? "当前数据集没有可用 Profile"
          : "请填写随机题数（1 到数据集题数之间）");
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
          const run = await createDirectLlmRun(buildRunRequest(model, selection));
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
    <div className="page operate">
      {/* 签发台：本次运行定义 / 模型 / 数据集维护。顶栏页签已经报了「Direct LLM / 操作」，页内不再重复标题。 */}
      <section className="panel" aria-label="本次运行定义">
        <div className="panel-head">
          <h2>本次运行定义</h2>
          <p className="panel-summary">
            本次将跑 <b className="mono">{runSize}</b> 题 · 已选 <b className="mono">{selected.length}</b> 个模型
          </p>
        </div>
        <FieldGrid>
          <Field label="数据集（版本固定）">
            {preset ? (
              <>
                {presets.length > 1 && (
                  <select
                    className="control"
                    aria-label="运行数据集"
                    value={preset.scenario}
                    onChange={(change) => {
                      setChosenScenario(change.target.value);
                      setCaseMode((current) => current === "profile" ? "all" : current);
                      setProfileName("");
                    }}
                  >
                    {presets.map((item) => (
                      <option key={item.scenario} value={item.scenario}>
                        {item.scenario} · {item.cases} 题
                      </option>
                    ))}
                  </select>
                )}
                {/* 数据集身份与事实合成一行：identity 加粗，其余跟在后面。
                     dataset 与 scenario 同名时只写一次，不复述。 */}
                <span className="hint mono dataset-line">
                  <b className="mono">{preset.scenario}</b>
                  {preset.dataset && preset.dataset !== preset.scenario ? " · " + preset.dataset : ""}
                  {" · "}{preset.cases} 题 · {scorerLabel(datasetScorer(preset))}
                </span>
                <span className="hint mono">
                  来源 {String(preset.provenance?.source ?? "—")}
                  {preset.eval?.prompt_version ? ` · prompt ${preset.eval.prompt_version}` : ""}
                  {preset.eval?.scorer_version ? ` · scorer ${preset.eval.scorer_version}` : ""}
                </span>
              </>
            ) : (
              <span className="hint">尚未导入数据集。用「内置样例」一键导入，或粘贴 / 选择本地 JSONL。</span>
            )}
          </Field>

          <Field label="本次运行题目">
            <select
              className="control"
              value={caseMode}
              onChange={(change) => {
                const nextMode: CaseMode = change.target.value === "ids" ? "ids"
                  : change.target.value === "random" ? "random"
                    : change.target.value === "profile" ? "profile" : "all";
                setCaseMode(nextMode);
                if (nextMode === "profile" && activeProfile) setProfileName(activeProfile.name);
              }}
            >
              <option value="all">全部（{preset?.cases ?? 0} 题）</option>
              {profiles.length > 0 && <option value="profile">数据集 Profile</option>}
              <option value="random">随机 N 题</option>
              <option value="ids">指定题目（题目页勾选）</option>
            </select>
          </Field>
          {caseMode === "random" && (
            <>
              <Field label="随机题数">
                <input
                  type="number" min={1} max={preset?.cases ?? 1} value={randomCount}
                  onChange={(change) => setRandomCount(change.target.value)}
                />
              </Field>
              <Field label="随机种子" hint="写进运行快照，可复现">
                <input
                  className="mono"
                  aria-label="随机种子（写进运行快照，可复现）"
                  value={seed}
                  onChange={(change) => setSeed(change.target.value)}
                />
              </Field>
              <button type="button" className="link col-span-full" onClick={() => setSeed(randomSeed())}>
                重新生成种子
              </button>
            </>
          )}
          {caseMode === "profile" && activeProfile && (
            <>
              <Field label="数据集 Profile">
                <select
                  className="control mono"
                  aria-label="运行 Profile"
                  value={activeProfile.name}
                  onChange={(change) => setProfileName(change.target.value)}
                >
                  {profiles.map((profile) => (
                    <option key={profile.name} value={profile.name}>
                      {profile.name} · {profile.count} 题
                    </option>
                  ))}
                </select>
              </Field>
              <p className="hint mono col-span-full">
                strategy {activeProfile.strategy ?? "未提供"}
                {activeProfile.case_ids_sha256 ? ` · case ids ${activeProfile.case_ids_sha256}` : ""}
              </p>
            </>
          )}
          {caseMode === "ids" && (
            <p className={pickedMismatch ? "error col-span-full" : "hint col-span-full"}>
              {usablePicked
                ? `已选 ${usablePicked.caseIds.length} 题（${usablePicked.dataset}）`
                : pickedUnavailable
                  ? `已选题目来自 ${picked?.dataset}，但该数据集当前不可用；请回题目页重新选择。`
                  : pickedMismatch
                    ? `已选题目来自 ${picked?.dataset}，当前数据集是 ${preset?.dataset}；不能用旧题目发起评测。`
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
            <p className="hint col-span-full">
              <Link className="link" to={ROUTES.cases}>浏览 / 勾选题目</Link>
            </p>
          )}

          <Field label="temperature">
            <input
              type="number" step="0.1" min="0" value={temperature}
              onChange={(change) => setTemperature(change.target.value)}
              placeholder="留空 = 模型档案默认值"
            />
          </Field>
          <Field label="max_output_tokens">
            <input
              type="number" min={1} value={budget}
              onChange={(change) => setBudget(change.target.value)}
              placeholder={`留空 = 数据集预设 ${presetBudget}`}
            />
          </Field>
        </FieldGrid>

        <details className="disclosure">
          <summary>
            受管来源{sourceError ? "（加载失败）" : managedSources == null ? "（加载中）" : `（${visibleSources?.length ?? 0}）`}
          </summary>
          <p className="hint">只读治理目录，不触发外部网络访问；待审核与受限来源不提供获取或准备操作。</p>
          {managedSources?.some((item) => item.status === "restricted") && (
            <div className="model-picker-item">
              <input
                type="checkbox"
                checked={showRestrictedSources}
                onChange={(change) => setShowRestrictedSources(change.target.checked)}
                aria-label="显示受限来源"
              />
              <span>显示受限来源</span>
            </div>
          )}
          {sourceError ? (
            <p className="error">受管来源加载失败：{sourceError}</p>
          ) : managedSources == null ? (
            <p className="empty">受管来源加载中</p>
          ) : managedSources.length === 0 ? (
            <p className="empty">暂无受管来源</p>
          ) : visibleSources?.length === 0 ? (
            <p className="empty">没有非受限来源</p>
          ) : (
            <Board
              label="受管来源目录"
              head={<>
                <th>来源</th><th>状态</th><th>Revision</th><th>Stable</th>
              </>}
            >
              {(visibleSources ?? []).map((managedSource) => {
                const statusMeta = SOURCE_STATUS_META[managedSource.status]
                  ?? { label: managedSource.status, tone: "neutral" as const };
                const isOpen = openSourceId === managedSource.id;
                return (
                  <Fragment key={managedSource.id}>
                    <tr>
                      <td>
                        <button
                          type="button"
                          className="link"
                          aria-expanded={isOpen}
                          onClick={() => toggleSourceDetail(managedSource.id)}
                        >
                          {managedSource.label}
                        </button>
                        <span className="muted mono"> {managedSource.id}</span>
                      </td>
                      <td>
                        <span className={`status-badge status-tone-${statusMeta.tone}`}>
                          {statusMeta.label}
                        </span>
                      </td>
                      <td>
                        <span
                          className={`status-badge ${managedSource.revision ? "status-tone-success" : "status-tone-neutral"}`}
                          title={managedSource.revision ?? undefined}
                        >
                          {managedSource.revision ? "已固定" : "未固定"}
                        </span>
                      </td>
                      <td>
                        <span className={`status-badge ${managedSource.stable_eligible ? "status-tone-success" : "status-tone-neutral"}`}>
                          {managedSource.stable_eligible ? "符合" : "不符合"}
                        </span>
                      </td>
                    </tr>
                    <tr>
                      <td colSpan={4} className="muted">
                        License <span className="mono">{managedSource.license_ids?.join(", ") || "—"}</span>
                        {" · "}Profiles <span className="mono">{managedSource.profiles?.join(", ") || "—"}</span>
                        {" · "}阻断 <span className="mono">{managedSource.blocker_count}</span>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="drill-detail-row">
                        <td colSpan={4}>
                          <div className="drill-detail">
                            {sourceDetailLoading ? (
                              <p className="empty">来源详情加载中</p>
                            ) : sourceDetailError ? (
                              <p className="error">来源详情加载失败：{sourceDetailError}</p>
                            ) : sourceDetail?.id === managedSource.id ? (
                              <>
                                <p>{sourceDetail.description}</p>
                                <dl className="kv">
                                  <dt>distribution scope</dt>
                                  <dd>
                                    {DISTRIBUTION_LABELS[sourceDetail.governance.distribution_scope]
                                      ?? sourceDetail.governance.distribution_scope}
                                    {" · "}<span className="mono">{sourceDetail.governance.distribution_scope}</span>
                                  </dd>
                                  <dt>revision</dt>
                                  <dd className="mono">
                                    {sourceDetail.upstream.revision.kind} · {sourceDetail.upstream.revision.value ?? "未固定"}
                                  </dd>
                                  <dt>comparability</dt>
                                  <dd>
                                    {COMPARABILITY_LABELS[sourceDetail.official_comparability.status]
                                      ?? sourceDetail.official_comparability.status}
                                  </dd>
                                  <dt>network_entrypoint</dt><dd className="mono">{sourceDetail.safety.network_entrypoint}</dd>
                                  <dt>trust_remote_code</dt><dd className="mono">{String(sourceDetail.safety.trust_remote_code)}</dd>
                                  <dt>online_rows_fallback</dt><dd className="mono">{String(sourceDetail.safety.online_rows_fallback)}</dd>
                                  <dt>executable_upstream_code</dt><dd className="mono">{String(sourceDetail.safety.executable_upstream_code)}</dd>
                                  <dt>archive_auto_extract</dt><dd className="mono">{String(sourceDetail.safety.archive_auto_extract)}</dd>
                                </dl>
                                <p>
                                  <span className="field-label">可比性说明</span>
                                  {sourceDetail.official_comparability.notes}
                                </p>
                                <p><span className="field-label">阻断项</span></p>
                                {sourceDetail.blockers.length > 0 ? (
                                  <ul>{sourceDetail.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>
                                ) : (
                                  <p className="hint">无阻断项</p>
                                )}
                              </>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </Board>
          )}
        </details>

        {/* 只读派生读数：不是可编辑字段，单独三列排开，不跟可编辑字段抢两列 */}
        <FieldGrid columns={3}>
          <Field label="题数">
            <span className="field-value mono">
              {caseMode === "random" ? `${randomCount || 0} / ${preset?.cases ?? 0}`
                : caseMode === "ids" ? `${usablePicked?.caseIds.length ?? 0} / ${preset?.cases ?? 0}`
                  : caseMode === "profile" ? `${activeProfile?.count ?? 0} / ${preset?.cases ?? 0}`
                    : preset?.cases ?? "—"}
            </span>
          </Field>
          <Field label="输出上限">
            <span className="field-value mono">{Number.isFinite(effectiveBudget) ? effectiveBudget : "—"}</span>
          </Field>
          <Field label="重试"><span className="field-value mono">0</span></Field>
        </FieldGrid>
        <p className="hint">
          输出上限不得超模型档案声明的上限；评分只看输出正文，流式思考会占满预算。
        </p>
      </section>

      <section className="panel" aria-label="模型">
        <div className="panel-head">
          <h2>模型（可多选对比）</h2>
          <p className="panel-summary">
            已选 <b className="mono">{selected.length}</b> 个
          </p>
        </div>
        <ModelPicker
          models={models}
          selected={selected}
          onToggle={(id) => setSelected((current) =>
            current.includes(id) ? current.filter((item) => item !== id) : [...current, id])}
        />
        {reasoningModels.length > 0 && (
          <FieldGrid>
            {reasoningModels.map((model) => (
              <Field
                key={model.id}
                label={model.id + " 的思考强度"}
                hint={"默认 " + (model.reasoning?.default_level ?? "无")}
              >
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
              </Field>
            ))}
          </FieldGrid>
        )}
      </section>

      <section className="panel" aria-label="数据集维护">
        <div className="panel-head">
          <h2>导入数据集</h2>
        </div>

        <p className="embed-title">内置样例</p>
        <p className="hint">
          仓库自带的小型样例集（见 datasets/direct-llm/），用来在零外部数据的前提下跑通链路。
        </p>
        {builtins.length > 0 ? (
          <form onSubmit={importBuiltin} aria-label="导入内置样例">
            <FieldGrid>
              <Field label="样例数据集" hint={usableBuiltin?.description ?? "该样例不可导入。"}>
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
              </Field>
              <Field label="数据集版本">
                <input
                  value={builtinVersion}
                  onChange={(change) => setBuiltinVersion(change.target.value)}
                  placeholder="留空 = 自动（同内容复用，否则下一个空号）"
                />
              </Field>
            </FieldGrid>
            <div className="actions">
              <button type="submit" disabled={importing || !usableBuiltin}>
                {importing ? "导入中…" : "导入所选样例"}
              </button>
            </div>
          </form>
        ) : (
          <p className="hint">内置样例清单不可用（服务端样例目录缺失）。</p>
        )}

        <p className="embed-title">导入本地 JSONL</p>
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
          <FieldGrid>
            <Field label="JSONL 内容" wide>
              <textarea
                className="mono"
                rows={6}
                value={content}
                onChange={(change) => setContent(change.target.value)}
                placeholder={'{"input": "1+1=?", "expected": "2"}'}
              />
            </Field>
          </FieldGrid>
          <details className="disclosure">
            <summary>高级设置</summary>
            <FieldGrid>
              <Field label="数据集名">
                <input
                  value={name}
                  onChange={(change) => setName(change.target.value)}
                  placeholder="留空 = direct-llm-custom"
                />
              </Field>
              <Field label="数据集版本">
                <input
                  value={version}
                  onChange={(change) => setVersion(change.target.value)}
                  placeholder="留空 = 自动（同内容复用，否则下一个空号）"
                />
              </Field>
              <Field label="默认评分器">
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
              </Field>
              <Field label="License">
                <input value={license} onChange={(change) => setLicense(change.target.value)} />
              </Field>
              <Field label="来源标记">
                <input
                  value={source}
                  onChange={(change) => setSource(change.target.value)}
                  placeholder="留空 = local-jsonl"
                />
              </Field>
            </FieldGrid>
          </details>
          <div className="actions">
            <button type="submit" disabled={importing || !content.trim()}>
              <UploadSimpleIcon size={14} weight="bold" aria-hidden />
              {importing ? "导入中…" : "导入 JSONL"}
            </button>
          </div>
          {message && <p className="import-feedback pass">{message}</p>}
        </form>
      </section>

      <IssueBar note={<>
        真实调用 · 产生费用 · 发起后自动进入过程页
        {selected.length > 1 ? " · 估算需只选择一个模型" : ""}
      </>}>
        <button
          type="button"
          className="primary"
          onClick={() => void doRun()}
          disabled={running || estimating || selected.length === 0 || !preset || runSize <= 0}
        >
          {running ? "创建中…" : `发起评测（${selected.length} 个模型 × ${runSize} 题）`}
        </button>
        <button
          type="button"
          onClick={() => void estimateRun()}
          disabled={estimating || running || selected.length !== 1 || !preset || runSize <= 0}
        >
          <CalculatorIcon size={14} weight="bold" aria-hidden />
          {estimating ? "估算中…" : "运行前估算"}
        </button>
      </IssueBar>
      {dryRunError && <p className="error" role="alert">运行前估算失败：{dryRunError}</p>}
      {dryRun && (
        <>
          <div className="panel-head">
            <h2>运行前估算</h2>
          </div>
          <dl className="kv" aria-label="运行前估算结果">
            <dt>选择题数</dt><dd className="mono">{dryRun.selected_count}</dd>
            <dt>单题输入上界</dt><dd className="mono">{dryRun.max_input_tokens_upper_bound ?? "未提供"}</dd>
            <dt>单题输出上限</dt><dd className="mono">{dryRun.max_output_tokens}</dd>
            <dt>单题总量上界</dt><dd className="mono">{dryRun.max_total_tokens_upper_bound ?? "未提供"}</dd>
            <dt>模型上下文</dt><dd className="mono">{dryRun.context_window ?? "未提供"}</dd>
            <dt>估算方法</dt><dd className="mono">{dryRun.estimation_method ?? "未提供"}</dd>
            <dt>费用上界</dt>
            <dd className="mono">
              {dryRun.estimated_cost_upper_bound == null
                ? "未提供"
                : `${dryRun.estimated_cost_upper_bound} ${dryRun.currency ?? "币种未提供"}`}
            </dd>
            <dt>价表版本</dt><dd className="mono">{dryRun.price_table_version ?? "未提供"}</dd>
          </dl>
          <p className="hint">估算结果是保守上界，不是最终账单；不会创建运行。</p>
        </>
      )}
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
      {error && <p className="error">{error}</p>}
    </div>
  );
}
