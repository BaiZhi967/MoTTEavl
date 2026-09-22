import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeftIcon, MagnifyingGlassIcon, PlayIcon, XIcon } from "@phosphor-icons/react";
import Input from "@douyinfe/semi-ui/lib/es/input";
import Button from "@douyinfe/semi-ui/lib/es/button";
import {
  getBenchmarkCases, getBenchmarkOverview, type BenchmarkCase, type BenchmarkPreset,
} from "../../api/client";
import { Board } from "../../board/Board";
import { EmptyBoard } from "../../board/EmptyBoard";
import { IssueBar } from "../../board/FieldGrid";
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
    <div className="page fill">
      <section className="panel detail" aria-label="GSM8K 题目">
        {/* 工具条：读数居左、过滤与批量操作居右；页面身份由顶栏页签承担，不再重复标题 */}
        <div className="panel-head">
          <p className="panel-summary">
            <span className="selection-count">已选 {selected.length} 题</span>
            {page && (
              <span className="hint mono">
                匹配 {page.total} / 数据集 {page.dataset_total} 题 · 第 {currentPage}/{pages} 页
              </span>
            )}
          </p>
          <div className="panel-head-actions">
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
              <Input
                value={pendingQuery}
                onChange={(value) => setPendingQuery(value)}
                placeholder="题面关键词或 case id"
                aria-label="搜索题目关键词"
              />
              <Button htmlType="submit">
                <MagnifyingGlassIcon size={14} weight="bold" aria-hidden />
                搜索
              </Button>
              {query && (
                <button type="button" className="link" onClick={() => { setPendingQuery(""); setQuery(""); setOffset(0); }}>
                  清除
                </button>
              )}
            </form>
            <Button onClick={togglePage} disabled={pageIds.length === 0}>
              {allOnPageSelected ? "取消本页" : "全选本页"}
            </Button>
            <Button
              onClick={() => { setSelected([]); clearCaseSelection(); setNotice(""); }}
              disabled={selected.length === 0}
            >
              清空已选
            </Button>
            <Button onClick={() => navigate(ROUTES.operate)}>
              <ArrowLeftIcon size={14} weight="bold" aria-hidden />
              返回操作页
            </Button>
          </div>
        </div>
        {error && <p className="error">{error}</p>}
        {notice && <p className="hint">{notice}</p>}

        <Board
          label="题目板面"
          head={
            <>
              <th className="w-[44px]" aria-label="选择" />
              <th className="w-[168px]">Case</th>
              <th>题目</th>
              <th className="w-[220px]">期望</th>
            </>
          }
        >
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
              <td className="data board-id">{item.case_id}</td>
              <td className="truncate" title={item.input}>{item.input}</td>
              <td className="data truncate" title={item.expected}>{item.expected}</td>
            </tr>
          ))}
          {page && page.items.length === 0 && (
            <tr>
              <td colSpan={4} style={{ height: "auto", padding: "16px" }}>
                <EmptyBoard
                  reason={query ? `没有匹配「${query}」的题目` : "这个数据集还没有题目"}
                  next={query ? "换关键词，或清除搜索看全部" : "先回操作页导入数据集"}
                />
              </td>
            </tr>
          )}
        </Board>

        {/* 翻页与粘贴：板面下方的次级操作带，不挤占板面宽度 */}
        <div className="board-foot">
          <Button onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0}>
            上一页
          </Button>
          <Button onClick={() => setOffset(offset + PAGE_SIZE)} disabled={currentPage >= pages}>
            下一页
          </Button>
          <span className="board-foot-sep" aria-hidden />
          <span className="field-label">粘贴 case ID</span>
          <Input
            value={paste}
            onChange={(value) => setPaste(value)}
            placeholder="gsm8k-test-0001, gsm8k-test-0042"
            aria-label="粘贴 case ID"
          />
          <Button onClick={addPasted} disabled={!paste.trim()}>加入</Button>
          <span className="hint">按 case id 精确匹配数据集，可一次粘贴多个</span>
        </div>

        <IssueBar note="回到操作页后选择模型与思考强度再发起；真实调用 · 产生费用">
          <Button
            theme="solid"
            type="primary"
            onClick={launch}
            disabled={selected.length === 0 || !preset}
          >
            <PlayIcon size={14} weight="bold" aria-hidden />
            用所选 {selected.length} 题发起跑测
          </Button>
        </IssueBar>
      </section>
    </div>
  );
}
