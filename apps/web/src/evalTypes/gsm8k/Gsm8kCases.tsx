import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeftIcon, MagnifyingGlassIcon, PlayIcon, XIcon } from "@phosphor-icons/react";
import {
  getBenchmarkCases, getBenchmarkOverview, type BenchmarkCase, type BenchmarkPreset,
} from "../../api/client";
import { suiteRoutes } from "../registry";
import { scopeLabel, sortPresets } from "./presets";
import { clearCaseSelection, loadCaseSelection, MAX_CASE_IDS, saveCaseSelection } from "./selection";

const ROUTES = suiteRoutes("gsm8k");
const PAGE_SIZE = 25;

/** 契约保证 case_id 形如 gsm8k-test-0000 且按序号连续，因此客户端可精确校验粘贴的 id。 */
function parseCaseIds(raw: string, datasetSize: number): { ids: string[]; problems: string[] } {
  const ids: string[] = [];
  const problems: string[] = [];
  for (const item of raw.split(/[,，\s]+/).filter(Boolean)) {
    const match = /^gsm8k-test-(\d{4,})$/.exec(item);
    if (!match || Number(match[1]) >= datasetSize) problems.push(item);
    else ids.push(item);
  }
  return { ids, problems };
}

export function Gsm8kCases() {
  const navigate = useNavigate();
  const [presets, setPresets] = useState<BenchmarkPreset[]>([]);
  const [dataset, setDataset] = useState("");
  const [query, setQuery] = useState("");
  const [pendingQuery, setPendingQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<{ total: number; dataset_total: number; items: BenchmarkCase[] } | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [paste, setPaste] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const preset = presets.find((item) => item.dataset === dataset) ?? presets[0];

  useEffect(() => {
    getBenchmarkOverview()
      .then((payload) => setPresets(sortPresets(payload.items)))
      .catch((e) => setError(String(e)));
    const stored = loadCaseSelection();
    if (stored) setSelected(stored.caseIds);
  }, []);

  useEffect(() => {
    if (!dataset && presets.length > 0) setDataset(presets[0].dataset);
  }, [presets, dataset]);

  const load = useCallback(async () => {
    if (!dataset) return;
    try {
      const payload = await getBenchmarkCases({ dataset, offset, limit: PAGE_SIZE, query });
      setPage({ total: payload.total, dataset_total: payload.dataset_total, items: payload.items });
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, [dataset, offset, query]);

  useEffect(() => {
    void load();
  }, [load]);

  const pageIds = useMemo(() => (page?.items ?? []).map((item) => item.case_id), [page]);
  const allOnPageSelected = pageIds.length > 0 && pageIds.every((caseId) => selected.includes(caseId));
  const pages = page ? Math.max(1, Math.ceil(page.total / PAGE_SIZE)) : 1;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  const toggle = (caseId: string) =>
    setSelected((current) => current.includes(caseId)
      ? current.filter((item) => item !== caseId)
      : [...current, caseId]);

  const togglePage = () =>
    setSelected((current) => allOnPageSelected
      ? current.filter((caseId) => !pageIds.includes(caseId))
      : [...new Set([...current, ...pageIds])]);

  const submitSearch = (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setOffset(0);
    setQuery(pendingQuery.trim());
  };

  const addPasted = () => {
    if (!preset) return;
    const { ids, problems } = parseCaseIds(paste, preset.cases);
    const merged = [...new Set([...selected, ...ids])].slice(0, MAX_CASE_IDS);
    setSelected(merged);
    setNotice(problems.length > 0
      ? `已加入 ${merged.length - selected.length} 题；忽略 ${problems.length} 个不在该数据集内的 id：${problems.slice(0, 5).join("、")}`
      : "");
    setPaste("");
  };

  const launch = () => {
    if (!preset || selected.length === 0) return;
    saveCaseSelection({ dataset: preset.dataset, caseIds: selected });
    navigate(ROUTES.operate);
  };

  return (
    <div className="page">
      <section className="panel detail" aria-label="GSM8K 题目">
        <div className="panel-head">
          <h2>GSM8K · 题目</h2>
          <div className="panel-head-actions">
            <button type="button" onClick={() => navigate(ROUTES.operate)}>
              <ArrowLeftIcon size={14} weight="bold" aria-hidden />
              返回操作页
            </button>
          </div>
        </div>
        <p className="hint">
          浏览数据集里的全部题目（题面与期望答案只读，数据集版本不可变）。勾选后回到操作页，题目卡会自动切到
          「指定题目」；也可以用「随机 N 题」按种子抽样。
        </p>
        {error && <p className="error">{error}</p>}

        <div className="inline-field">
          <span className="field-label">数据集</span>
          <select
            className="control"
            aria-label="题目数据集"
            value={preset?.dataset ?? ""}
            onChange={(change) => { setDataset(change.target.value); setOffset(0); }}
          >
            {presets.map((item) => (
              <option key={item.dataset} value={item.dataset}>
                {item.dataset} · {scopeLabel(item.scope)} · {item.cases} 题
              </option>
            ))}
          </select>
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
          <button type="button" onClick={() => { setSelected([]); clearCaseSelection(); setNotice(""); }} disabled={selected.length === 0}>
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
              <th>题目</th>
              <th>期望</th>
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
                <td className="mono">{item.expected}</td>
              </tr>
            ))}
            {page && page.items.length === 0 && (
              <tr><td colSpan={4} className="empty">没有匹配的题目</td></tr>
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
            placeholder="gsm8k-test-0001, gsm8k-test-0042"
            aria-label="粘贴 case ID"
          />
          <button type="button" onClick={addPasted} disabled={!paste.trim()}>
            加入
          </button>
        </div>
        <p className="hint">粘贴按 case id 精确匹配数据集（如 gsm8k-test-0001），可一次粘贴多个。</p>

        <div className="actions">
          <button type="button" onClick={launch} disabled={selected.length === 0 || !preset}>
            <PlayIcon size={14} weight="bold" aria-hidden />
            用所选 {selected.length} 题发起跑测
          </button>
          <span className="hint">回到操作页后选择模型与思考强度再发起；真实调用 · 产生费用</span>
        </div>
        {selected.length > 0 && (
          <p className="hint">
            已选题目来自 {preset?.dataset}；操作页若切到别的数据集，需要回来重新选择。
            <button type="button" className="link" onClick={() => { setSelected([]); clearCaseSelection(); }}>
              <XIcon size={12} weight="bold" aria-hidden /> 清空
            </button>
          </p>
        )}
      </section>
    </div>
  );
}
