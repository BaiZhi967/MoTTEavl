import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeftIcon } from "@phosphor-icons/react";
import {
  compareRuns,
  createExternalRun,
  evaluateRunGate,
  getExternalCases,
  getExternalJobs,
  getExternalPreflight,
  getExternalCatalog,
  getRuns,
  getRun,
  prepareExternalDataset,
  type ExternalJobRecord,
} from "../../api/client";
import { suiteRoutes } from "../registry";

const SCOPE_LABELS: Record<string, string> = {
  smoke: "smoke（小样本）",
  "custom-subset": "custom-subset（自选子集）",
  full: "full（完整 Profile）",
};

export function scopeLabel(scope: string | undefined): string {
  return SCOPE_LABELS[scope ?? "custom-subset"] ?? scope ?? "custom-subset";
}

export interface ExternalBenchmarkLabels {
  title: string;
  operateAria: string;
  preparePlaceholder: string;
}

const DEFAULT_LABELS: ExternalBenchmarkLabels = {
  title: "外部基准（job-based）",
  operateAria: "外部基准",
  preparePlaceholder: "每行一题 JSON（id/subject/question/A-D/answer；answer 缺省=unscored）",
};

/**
 * 生成一个外部 job-based 基准的五页组件（ceval/cmmlu 共用，review R16）。
 *
 * 异步身份保护（review R15）：结果页/监控页的每次加载绑定请求代号，切
 * runId 立即清空旧状态，晚到的旧响应被丢弃；比较页的每次提交绑定提交
 * 代号，反序返回的 compare/gate 不覆盖第二组结果，输入改动即清理旧结论。
 */
export function makeExternalPages(benchmarkId: string, labels: Partial<ExternalBenchmarkLabels> = {}) {
  const text = { ...DEFAULT_LABELS, ...labels };
  const ROUTES = suiteRoutes(benchmarkId);

  function BackLink() {
    const navigate = useNavigate();
    return (
      <button type="button" className="link" onClick={() => navigate(ROUTES.operate)}>
        <ArrowLeftIcon size={14} weight="bold" /> 返回操作页
      </button>
    );
  }

  /** 操作页：Catalog 四态、数据准备（来源治理）、静态预检、创建运行。 */
  function Operate() {
    const navigate = useNavigate();
    const [catalog, setCatalog] = useState<{ status: string; blockers: string[]; dataset: any } | null>(null);
    const [revision, setRevision] = useState(`rev-${benchmarkId}-1`);
    const [fileText, setFileText] = useState("");
    const [prepareNotice, setPrepareNotice] = useState("");
    const [error, setError] = useState("");
    const [preflight, setPreflight] = useState<{ ok: boolean; reasons: string[]; checks: Record<string, boolean | null> } | null>(null);
    const [model, setModel] = useState("");
    const [scope, setScope] = useState("custom-subset");
    const [runError, setRunError] = useState("");

    const refresh = () => getExternalCatalog()
      .then((payload) => {
        const entry = payload.benchmarks.find((item) => item.benchmark_id === benchmarkId) ?? null;
        setCatalog(entry);
      })
      .catch((e) => setError(String(e)));

    useEffect(() => { refresh(); }, []);

    const submitPrepare = (event: FormEvent) => {
      event.preventDefault();
      setPrepareNotice("");
      setError("");
      const files: Record<string, string> = {};
      for (const block of fileText.split(/```|\n---\n/)) {
        const nameMatch = /^#\s*(\S+)/.exec(block);
        if (nameMatch) files[nameMatch[1]] = block.slice(block.indexOf("\n") + 1);
      }
      if (Object.keys(files).length === 0) {
        files[`${benchmarkId}_data.jsonl`] = fileText;
      }
      prepareExternalDataset(benchmarkId, { files, dataset_revision: revision })
        .then((payload) => {
          setPrepareNotice(`已准备 ${payload.rows} 题（${payload.provenance}${payload.unscored ? "，unscored" : ""}）`);
          refresh();
        })
        .catch((e) => setError(String(e)));
    };

    const submitPreflight = (event: FormEvent) => {
      event.preventDefault();
      getExternalPreflight(benchmarkId, { model, scope })
        .then(setPreflight)
        .catch((e) => setError(String(e)));
    };

    const submitRun = (event: FormEvent) => {
      event.preventDefault();
      setRunError("");
      createExternalRun(benchmarkId, { model, scope })
        .then((run) => navigate(ROUTES.monitor([run.id])))
        .catch((e) => setRunError(String(e)));
    };

    return (
      <div className="page">
        <section className="panel detail" aria-label={text.operateAria}>
          <div className="panel-head"><h2>{text.title}</h2></div>
          {error && <p className="error">{error}</p>}
          <p className="hint">
            Catalog 状态：<strong data-testid="catalog-status">{catalog?.status ?? "…"}</strong>
            {catalog?.blockers?.length ? `；阻塞：${catalog.blockers.join("、")}` : ""}
            {catalog?.dataset ? `（来源 ${catalog.dataset.provenance}，${catalog.dataset.rows} 题）` : ""}
          </p>
          <p className="hint">一次 Run 只启动一个外部 Job；Runner 未注册/数据未准备时创建会在提交前被拒绝，不产生模型调用。</p>

          <form className="inline-field" onSubmit={submitPrepare} aria-label="准备本地数据">
            <span className="field-label">数据准备</span>
            <input className="control" value={revision} onChange={(e) => setRevision(e.target.value)} aria-label="数据 revision" />
            <textarea className="control" rows={4} value={fileText} onChange={(e) => setFileText(e.target.value)}
              placeholder={text.preparePlaceholder} aria-label="JSONL 内容" />
            <button className="button" type="submit">校验并准备</button>
            {prepareNotice && <span className="hint" data-testid="prepare-notice">{prepareNotice}</span>}
          </form>

          <form className="inline-field" onSubmit={submitPreflight} aria-label="静态预检">
            <span className="field-label">静态预检</span>
            <input className="control" value={model} onChange={(e) => setModel(e.target.value)} placeholder="模型档案 id" aria-label="模型" />
            <button className="button" type="submit">预检（不触发模型调用）</button>
          </form>
          {preflight && (
            <div data-testid="preflight-panel">
              <p className="hint">{preflight.ok ? "预检通过" : `预检未通过：${preflight.reasons.join("；")}`}</p>
              <p className="hint">静态能力与真实已验证状态分开；打开页面/预检不会发起付费调用。</p>
            </div>
          )}

          <form className="inline-field" onSubmit={submitRun} aria-label="创建运行">
            <span className="field-label">创建运行</span>
            <select className="control" value={scope} onChange={(e) => setScope(e.target.value)} aria-label="范围">
              {Object.entries(SCOPE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
            <button className="button" type="submit">排队执行</button>
            {runError && <span className="error" data-testid="run-error">{runError}</span>}
          </form>
        </section>
      </div>
    );
  }

  /** 题目页：准备好的样本清单（学科、是否带 gold）。 */
  function Cases() {
    const [query, setQuery] = useState("");
    const [page, setPage] = useState<{ cases: { case_id: string; subject: string; has_gold: boolean }[]; total: number } | null>(null);
    const [error, setError] = useState("");
    const requestRef = useRef(0);

    useEffect(() => {
      // 搜索词每次变化都换发请求代号：慢的旧搜索不覆盖新结果（review R15）。
      const token = ++requestRef.current;
      setPage(null);
      getExternalCases(benchmarkId, query)
        .then((payload) => {
          if (token !== requestRef.current) return;
          setPage(payload);
        })
        .catch((e) => {
          if (token !== requestRef.current) return;
          setError(String(e));
        });
    }, [query]);

    return (
      <div className="page">
        <section className="panel detail" aria-label={`${benchmarkId} 题目`}>
          <div className="panel-head"><h2>题目</h2><BackLink /></div>
          {error && <p className="error">{error}</p>}
          <div className="inline-field">
            <span className="field-label">搜索</span>
            <input className="control" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="搜索" />
          </div>
          {page && page.cases.length > 0 ? (
            <table className="table" aria-label="样本清单">
              <thead><tr><th>case</th><th>学科</th><th>gold</th></tr></thead>
              <tbody>
                {page.cases.map((row) => (
                  <tr key={row.case_id}>
                    <td className="mono">{row.case_id}</td>
                    <td>{row.subject}</td>
                    <td>{row.has_gold ? "有" : "无（unscored）"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="hint">尚无已准备的数据集。</p>}
        </section>
      </div>
    );
  }

  /** 监控页：Run 列表 + 每个 Run 的外部 Job 状态。 */
  function Monitor() {
    const navigate = useNavigate();
    const [runs, setRuns] = useState<any[]>([]);
    const [jobs, setJobs] = useState<Record<string, ExternalJobRecord[]>>({});

    useEffect(() => {
      getRuns()
        .then((payload) => setRuns(
          payload.items.filter((run) => run.scenario_version?.startsWith(`${benchmarkId}-external@`)),
        ))
        .catch(() => setRuns([]));
    }, []);

    useEffect(() => {
      for (const run of runs.slice(0, 10)) {
        getExternalJobs(run.id)
          .then((payload) => setJobs((current) => ({ ...current, [run.id]: payload.jobs })))
          .catch(() => undefined);
      }
    }, [runs]);

    return (
      <div className="page">
        <section className="panel detail" aria-label={`${benchmarkId} 运行监控`}>
          <div className="panel-head"><h2>运行监控</h2><BackLink /></div>
          {runs.length === 0 && <p className="hint">暂无 {benchmarkId}-external 运行。</p>}
          <table className="table" aria-label="运行列表">
            <thead><tr><th>Run</th><th>状态</th><th>Job</th><th>scope</th></tr></thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id}>
                  <td className="mono">
                    <button type="button" className="link" onClick={() => navigate(ROUTES.result(run.id))}>{run.id}</button>
                  </td>
                  <td>{run.status}</td>
                  <td className="mono">{(jobs[run.id] ?? []).map((job) => job.status).join(",") || "—"}</td>
                  <td>{scopeLabel(run.manifest?.scope)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    );
  }

  /** 结果页：scope 常显、native/diagnostic 分栏、未尝试不消失、费用 unknown。 */
  function Result() {
    const { runId = "" } = useParams();
    const [run, setRun] = useState<any>(null);
    const [jobs, setJobs] = useState<ExternalJobRecord[]>([]);
    const [missing, setMissing] = useState(false);
    const requestRef = useRef(0);

    useEffect(() => {
      // 请求代号绑定 runId：切换后清空旧状态，晚到的旧响应被丢弃（R15）。
      const token = ++requestRef.current;
      setRun(null);
      setJobs([]);
      setMissing(false);
      getRun(runId).then((payload) => {
        if (token !== requestRef.current) return;
        setRun(payload);
      }).catch(() => {
        if (token !== requestRef.current) return;
        setMissing(true);
      });
      getExternalJobs(runId).then((payload) => {
        if (token !== requestRef.current) return;
        setJobs(payload.jobs);
      }).catch(() => {
        if (token !== requestRef.current) return;
        setJobs([]);
      });
    }, [runId]);

    const metrics = useMemo(() => {
      const checkpoint = jobs[0]?.metrics ?? {};
      return {
        native: checkpoint.ceval_native as Record<string, unknown> | undefined,
        diagnostic: checkpoint.ceval_diagnostic as Record<string, unknown> | undefined,
      };
    }, [jobs]);

    const cases = run?.cases ?? [];
    const notAttempted = cases.filter((row: any) => row.outcome === "not_attempted");
    const failed = cases.filter((row: any) => row.outcome === "call_failed");

    return (
      <div className="page">
        <section className="panel detail" aria-label={`${benchmarkId} 结果`}>
          <div className="panel-head"><h2>结果</h2><BackLink /></div>
          {!run && <p className="hint">{missing ? "运行不存在或已删除。" : "读取中…"}</p>}
          {run && (
            <>
              <p className="hint" data-testid="scope-label">
                范围：{scopeLabel(run.manifest?.scope)}；来源：{run.manifest?.external_benchmark?.dataset_revision ?? "—"}
              </p>
              <p className="hint" data-testid="job-status">
                外部 Job：{jobs.map((job) => `${job.job_id}:${job.status}`).join(", ") || "—"}；费用可见度：unknown（runner-native 调用路径）
              </p>
              {failed.length > 0 && (
                <p className="error" data-testid="partial-failure">
                  {failed.length} 条样本执行失败（部分结果已保留）。
                </p>
              )}
              {notAttempted.length > 0 && (
                <p className="hint" data-testid="not-attempted">
                  {notAttempted.length} 条未尝试（not_attempted，计入分母不消失）：{notAttempted.map((row: any) => row.case_id).join("、")}
                </p>
              )}
              <table className="table" aria-label="样本结果">
                <thead><tr><th>case</th><th>处置</th><th>预测</th><th>gold</th></tr></thead>
                <tbody>
                  {cases.map((row: any) => (
                    <tr key={row.case_id}>
                      <td className="mono">{row.case_id}</td>
                      <td>{row.outcome}</td>
                      <td className="mono">{row.result?.prediction ?? "—"}</td>
                      <td className="mono">{row.result?.gold ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div data-testid="metric-columns">
                <p className="hint">指标双栏（互不覆盖）：</p>
                <div className="inline-field">
                  <table className="table" aria-label="native 指标">
                    <thead><tr><th>native.*（Runner 原始聚合）</th><th>值</th></tr></thead>
                    <tbody>
                      {Object.entries(metrics.native ?? {}).slice(0, 8).map(([key, value]) => (
                        <tr key={key}><td className="mono">{key}</td><td className="mono">{String(value)}</td></tr>
                      ))}
                      {!metrics.native && (
                        <tr><td colSpan={2} className="hint">无原生分数输入时不生成 native 指标。</td></tr>
                      )}
                    </tbody>
                  </table>
                  <table className="table" aria-label="diagnostic 指标">
                    <thead><tr><th>diagnostic.*（平台重算）</th><th>值</th></tr></thead>
                    <tbody>
                      <tr>
                        <td className="mono">per_subject</td>
                        <td className="mono">{JSON.stringify(metrics.diagnostic?.per_subject ?? {})}</td>
                      </tr>
                      <tr>
                        <td className="mono">aggregate.accuracy</td>
                        <td className="mono">{String(metrics.diagnostic?.aggregate?.accuracy ?? "—")}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </>
          )}
        </section>
      </div>
    );
  }

  /** 比较页：逐条件原因；缺覆盖/不可比不显示放行。 */
  function Compare() {
    const [params] = useSearchParams();
    const initial = (params.get("runs") ?? "").split(",").filter(Boolean);
    const [baseline, setBaseline] = useState(initial[0] ?? "");
    const [candidate, setCandidate] = useState(initial[1] ?? "");
    const [comparison, setComparison] = useState<{ eligible: boolean; reasons: string[]; case_diff: any } | null>(null);
    const [gate, setGate] = useState<{ passed: boolean; rules: { id: string; passed: boolean; reason: string }[] } | null>(null);
    const [error, setError] = useState("");
    const submitRef = useRef(0);

    // 输入改动即清理旧结论：旧 gate 不随输入漂移显示（review R15）。
    useEffect(() => {
      setComparison(null);
      setGate(null);
      setError("");
    }, [baseline, candidate]);

    const submit = (event: FormEvent) => {
      event.preventDefault();
      const token = ++submitRef.current;
      setError("");
      setComparison(null);
      setGate(null);
      compareRuns(baseline, candidate)
        .then((payload) => {
          if (token !== submitRef.current) return;
          setComparison(payload);
          return evaluateRunGate({
            run_id: candidate,
            baseline_run_id: baseline,
            policy: {
              metric: "accuracy", op: "gte", threshold: 0.5,
              required_coverage: 1.0, require_cost_known: true, require_comparable: true,
            },
          }).then((gatePayload) => {
            if (token !== submitRef.current) return;
            setGate(gatePayload);
          });
        })
        .catch((e) => {
          if (token !== submitRef.current) return;
          setError(String(e));
        });
    };

    return (
      <div className="page">
        <section className="panel detail" aria-label={`${benchmarkId} 比较`}>
          <div className="panel-head"><h2>比较</h2><BackLink /></div>
          <form className="inline-field" onSubmit={submit} aria-label="选择运行">
            <span className="field-label">Baseline</span>
            <input className="control" value={baseline} onChange={(e) => setBaseline(e.target.value)} aria-label="baseline" />
            <span className="field-label">候选</span>
            <input className="control" value={candidate} onChange={(e) => setCandidate(e.target.value)} aria-label="candidate" />
            <button className="button" type="submit">比较</button>
          </form>
          {error && <p className="error">{error}</p>}
          {comparison && (
            <div data-testid="comparison-panel">
              <p className="hint">
                {comparison.eligible ? "两份报告可比。" : "不可比：" + comparison.reasons.join("；")}
              </p>
              <p className="hint">
                case 差异：+{comparison.case_diff.added.length} / -{comparison.case_diff.removed.length} / 改 {comparison.case_diff.changed.length}
              </p>
            </div>
          )}
          {gate && (
            <div data-testid="gate-panel">
              {gate.passed ? (
                <p className="hint" data-testid="gate-pass">门禁通过（同集同 Profile、覆盖与阈值达标）。</p>
              ) : (
                <div data-testid="gate-blocked">
                  <p className="error">门禁未通过，不显示放行：</p>
                  <ul>
                    {gate.rules.filter((rule) => !rule.passed).map((rule) => (
                      <li key={rule.id} className="hint">{rule.id}：{rule.reason}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </section>
      </div>
    );
  }

  return { Operate, Cases, Monitor, Result, Compare };
}
