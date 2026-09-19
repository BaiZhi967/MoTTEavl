import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeftIcon, MagnifyingGlassIcon, PlayIcon, XIcon } from "@phosphor-icons/react";
import {
  getDirectLlmCases, getDirectLlmOverview,
  type DirectLlmCase, type DirectLlmPreset,
} from "../../api/client";
import { MAX_CASE_IDS, clearCaseSelection, loadCaseSelection, saveCaseSelection } from "./selection";
import { suiteRoutes } from "../registry";
import { datasetScorer, scorerLabel, scorerShort, sortPresets } from "./presets";

const ROUTES = suiteRoutes("direct-llm");
const PAGE_SIZE = 25;

export function DirectLlmCases() {
  const navigate = useNavigate();
  const [presets, setPresets] = useState<DirectLlmPreset[]>([]);
  const [dataset, setDataset] = useState("");
  const [query, setQuery] = useState("");
  const [pendingQuery, setPendingQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<{ total: number; dataset_total: number; items: DirectLlmCase[] } | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [selectionDataset, setSelectionDataset] = useState<string | null>(null);
  const [paste, setPaste] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const preset = presets.find((item) => item.dataset === dataset);

  useEffect(() => {
    getDirectLlmOverview()
      .then((payload) => setPresets(sortPresets(payload.items)))
      .catch((e) => setError(String(e)));
    const stored = loadCaseSelection();
    if (stored) {
      setDataset(stored.dataset);
      setSelected(stored.caseIds);
      setSelectionDataset(stored.dataset);
    }
  }, []);

  useEffect(() => {
    if (presets.length === 0 || presets.some((item) => item.dataset === dataset)) return;
    const fallbackDataset = presets[0].dataset;
    setDataset(fallbackDataset);
    if (selectionDataset && selectionDataset !== fallbackDataset) {
      setSelected([]);
      setSelectionDataset(null);
      clearCaseSelection();
      setNotice(`先前选择的数据集 ${selectionDataset} 当前不可用，已清空已选题目。`);
    }
  }, [dataset, presets, selectionDataset]);

  useEffect(() => {
    if (!dataset || !preset) return;
    let active = true;
    getDirectLlmCases({ dataset, offset, limit: PAGE_SIZE, query })
      .then((payload) => {
        if (!active) return;
        setPage({ total: payload.total, dataset_total: payload.dataset_total, items: payload.items });
        setError("");
      })
      .catch((e) => {
        if (active) setError(String(e));
      });
    return () => { active = false; };
  }, [dataset, offset, preset, query]);

  const pageIds = useMemo(() => (page?.items ?? []).map((item) => item.case_id), [page]);
  const allOnPageSelected = pageIds.length > 0 && pageIds.every((caseId) => selected.includes(caseId));
  const pages = page ? Math.max(1, Math.ceil(page.total / PAGE_SIZE)) : 1;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  const replaceSelection = (caseIds: string[]) => {
    setSelected(caseIds);
    setSelectionDataset(caseIds.length > 0 ? dataset : null);
  };

  const clearSelection = () => {
    setSelected([]);
    setSelectionDataset(null);
    clearCaseSelection();
  };

  const toggle = (caseId: string) => replaceSelection(selected.includes(caseId)
    ? selected.filter((item) => item !== caseId)
    : [...selected, caseId]);

  const togglePage = () => replaceSelection(allOnPageSelected
    ? selected.filter((caseId) => !pageIds.includes(caseId))
    : [...new Set([...selected, ...pageIds])]);

  const submitSearch = (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setOffset(0);
    setQuery(pendingQuery.trim());
  };

  /* case_id 由导入方自由指定，客户端无法在分页下精确校验，因此原样收集、由服务端在发起时拒绝未知 id。 */
  const addPasted = () => {
    const ids = [...new Set(paste.split(/[,，\s]+/).filter(Boolean))];
    if (ids.length === 0) return;
    const merged = [...new Set([...selected, ...ids])].slice(0, MAX_CASE_IDS);
    replaceSelection(merged);
    setNotice(`已加入 ${ids.length} 个 id（共 ${merged.length} 个）；未知 id 会在发起时被服务端拒绝。`);
    setPaste("");
  };

  const launch = () => {
    if (!preset || selected.length === 0) return;
    if (selectionDataset !== preset.dataset) {
      setNotice(`已选题目属于 ${selectionDataset ?? "未知数据集"}，不能绑定到 ${preset.dataset}。`);
      return;
    }
    saveCaseSelection({ dataset: preset.dataset, caseIds: selected });
    navigate(ROUTES.operate);
  };

  return (
    <div className="page">
      <section className="panel detail" aria-label="Direct LLM 题目">
        <div className="panel-head">
          <h2>Direct LLM · 题目</h2>
          <div className="panel-head-actions">
            <button type="button" onClick={() => navigate(ROUTES.operate)}>
              <ArrowLeftIcon size={14} weight="bold" aria-hidden />
              返回操作页
            </button>
          </div>
        </div>
        <p className="hint">
          浏览数据集里的全部题目（题面、期望答案与生效评分器只读，数据集版本不可变）。勾选后回到操作页，
          题目卡会自动切到「指定题目」；也可以用「随机 N 题」按种子抽样。
        </p>
        {error && <p className="error">{error}</p>}

        <div className="inline-field">
          <span className="field-label">数据集</span>
          <select
            className="control"
            aria-label="题目数据集"
            value={dataset}
            onChange={(change) => {
              if (change.target.value !== dataset) {
                const hadSelection = selected.length > 0;
                clearSelection();
                setNotice(hadSelection ? "已切换数据集，原已选题目已清空。" : "");
              }
              setDataset(change.target.value);
              setPage(null);
              setOffset(0);
            }}
          >
            {presets.map((item) => (
              <option key={item.dataset} value={item.dataset}>
                {item.dataset} · {item.cases} 题 · {scorerShort(datasetScorer(item))}
              </option>
            ))}
          </select>
          {preset && <span className="hint">{scorerLabel(datasetScorer(preset))}（数据集默认，单题可覆盖）</span>}
        </div>

        <form className="inline-field" onSubmit={submitSearch} aria-label="搜索题目">
          <span className="field-label">搜索</span>
          <input
            className="control"
            value={pendingQuery}
            onChange={(change) => setPendingQuery(change.target.value)}
            placeholder="题面关键词或 case id"
            aria-label="搜索题目关键词"
          />
          <button type="submit">
            <MagnifyingGlassIcon size={14} weight="bold" aria-hidden />
            搜索
          </button>
          {query && (
            <button type="button" className="link" onClick={() => { setPendingQuery(""); setQuery(""); setOffset(0); }}>
              清除
            </button>
          )}
        </form>

        <div className="inline-field">
          <button type="button" onClick={togglePage} disabled={pageIds.length === 0}>
            {allOnPageSelected ? "取消本页" : "全选本页"}
          </button>
          <button type="button" onClick={() => { clearSelection(); setNotice(""); }} disabled={selected.length === 0}>
            清空已选
          </button>
          <span className="field-label">已选 {selected.length} 题</span>
          {page && (
            <span className="hint mono">
              匹配 {page.total} / 数据集 {page.dataset_total} 题 · 第 {currentPage}/{pages} 页
            </span>
          )}
        </div>

        {notice && <p className="hint">{notice}</p>}

        <table>
          <thead>
            <tr>
              <th aria-label="选择" />
              <th>Case</th>
              <th>题面</th>
              <th>期望</th>
              <th>评分器</th>
            </tr>
          </thead>
          <tbody>
            {(page?.items ?? []).map((item) => (
              <tr key={item.case_id}>
                <td>
                  <input
                    type="checkbox"
                    checked={selected.includes(item.case_id)}
                    onChange={() => toggle(item.case_id)}
                    aria-label={`选择 ${item.case_id}`}
                  />
                </td>
                <td className="mono nowrap">{item.case_id}</td>
                <td>{item.input}</td>
                <td className="mono">{item.expected ?? "（无判定）"}</td>
                <td className="mono">{scorerShort(item.scorer)}</td>
              </tr>
            ))}
            {page && page.items.length === 0 && (
              <tr><td colSpan={5} className="empty">没有匹配的题目</td></tr>
            )}
          </tbody>
        </table>

        <div className="actions">
          <button type="button" onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0}>
            上一页
          </button>
          <button type="button" onClick={() => setOffset(offset + PAGE_SIZE)} disabled={currentPage >= pages}>
            下一页
          </button>
        </div>

        <div className="inline-field">
          <span className="field-label">粘贴 case ID</span>
          <input
            className="control"
            value={paste}
            onChange={(change) => setPaste(change.target.value)}
            placeholder="direct-llm-classify-0001, direct-llm-classify-0004"
            aria-label="粘贴 case ID"
          />
          <button type="button" onClick={addPasted} disabled={!paste.trim()}>
            加入
          </button>
        </div>
        <p className="hint">粘贴按 case id 精确匹配，可一次粘贴多个（逗号或空白分隔）。</p>

        <div className="actions">
          <button
            type="button"
            onClick={launch}
            disabled={selected.length === 0 || !preset || selectionDataset !== preset.dataset}
          >
            <PlayIcon size={14} weight="bold" aria-hidden />
            用所选 {selected.length} 题发起评测
          </button>
          <span className="hint">回到操作页后选择模型与参数再发起；真实调用 · 产生费用</span>
        </div>
        {selected.length > 0 && (
          <p className="hint">
            已选题目来自 {selectionDataset}；操作页若切到别的数据集，需要回来重新选择。
            <button type="button" className="link" onClick={clearSelection}>
              <XIcon size={12} weight="bold" aria-hidden /> 清空
            </button>
          </p>
        )}
      </section>
    </div>
  );
}
