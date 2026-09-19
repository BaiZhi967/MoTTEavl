import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeftIcon } from "@phosphor-icons/react";
import {
  compareRuns,
  createCevalRun,
  evaluateRunGate,
  getCevalCases,
  getCevalPreflight,
  getExternalCatalog,
  getExternalJobs,
  getRuns,
  prepareCevalDataset,
  type ExternalJobRecord,
} from "../../api/client";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("ceval");

const SCOPE_LABELS: Record<string, string> = {
  smoke: "smoke（小样本）",
  "custom-subset": "custom-subset（自选子集）",
  full: "full（完整 Profile）",
};

export function scopeLabel(scope: string | undefined): string {
  return SCOPE_LABELS[scope ?? "custom-subset"] ?? scope ?? "custom-subset";
}

function BackLink() {
  const navigate = useNavigate();
  return (
    <button type="button" className="link" onClick={() => navigate(ROUTES.operate)}>
      <ArrowLeftIcon size={14} weight="bold" /> 返回操作页
    </button>
  );
}

/** 操作页：Catalog 四态、数据准备（来源治理）、静态预检、创建运行。 */
export function CevalOperate() {
  const navigate = useNavigate();
  const [catalog, setCatalog] = useState<{ status: string; blockers: string[]; dataset: any } | null>(null);
  const [revision, setRevision] = useState("rev-local-1");
  const [fileText, setFileText] = useState("");
  const [prepareNotice, setPrepareNotice] = useState("");
  const [error, setError] = useState("");
  const [preflight, setPreflight] = useState<{ ok: boolean; reasons: string[]; checks: Record<string, boolean | null> } | null>(null);
  const [model, setModel] = useState("");
  const [scope, setScope] = useState("custom-subset");
  const [runError, setRunError] = useState("");

  const refresh = () => getExternalCatalog()
    .then((payload) => {
      const entry = payload.benchmarks.find((item) => item.benchmark_id === "ceval") ?? null;
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
      files["logic_val.jsonl"] = fileText;
    }
    prepareCevalDataset({ files, dataset_revision: revision })
      .then((payload) => {
        setPrepareNotice(`已准备 ${payload.rows} 题（${payload.provenance}${payload.unscored ? "，unscored" : ""}）`);
        refresh();
      })
      .catch((e) => setError(String(e)));
  };

  const submitPreflight = (event: FormEvent) => {
    event.preventDefault();
    getCevalPreflight(model)
      .then(setPreflight)
      .catch((e) => setError(String(e)));
  };

  const submitRun = (event: FormEvent) => {
    event.preventDefault();
    setRunError("");
    createCevalRun({ model, scope })
      .then((run) => navigate(ROUTES.monitor([run.id])))
      .catch((e) => setRunError(String(e)));
  };

  return (
    <div className="page">
      <section className="panel detail" aria-label="C-Eval 外部基准">
        <div className="panel-head"><h2>C-Eval（job-based 外部基准）</h2></div>
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
            placeholder="每行一题 JSON（id/subject/question/A-D/answer；answer 缺省=unscored）" aria-label="JSONL 内容" />
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
export function CevalCases() {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState<{ cases: { case_id: string; subject: string; has_gold: boolean }[]; total: number } | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getCevalCases(query)
      .then(setPage)
      .catch((e) => setError(String(e)));
  }, [query]);

  return (
    <div className="page">
      <section className="panel detail" aria-label="C-Eval 题目">
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
export function CevalMonitor() {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<any[]>([]);
  const [jobs, setJobs] = useState<Record<string, ExternalJobRecord[]>>({});

  useEffect(() => {
    getRuns()
      .then((payload) => setRuns(
        payload.items.filter((run) => run.scenario_version?.startsWith("ceval-external@")),
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
      <section className="panel detail" aria-label="C-Eval 运行监控">
        <div className="panel-head"><h2>运行监控</h2><BackLink /></div>
        {runs.length === 0 && <p className="hint">暂无 ceval-external 运行。</p>}
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

/** 结果页：scope 常显、native/diagnostic 分开、未尝试不消失、费用 unknown。 */
export function CevalResult() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<any>(null);
  const [jobs, setJobs] = useState<ExternalJobRecord[]>([]);

  useEffect(() => {
    import("../../api/client").then((client) => {
      client.getRun(runId).then(setRun).catch(() => setRun(null));
      getExternalJobs(runId).then((payload) => setJobs(payload.jobs)).catch(() => setJobs([]));
    });
  }, [runId]);

  const cases = run?.cases ?? [];
  const notAttempted = cases.filter((row: any) => row.outcome === "not_attempted");
  const failed = cases.filter((row: any) => row.outcome === "call_failed");

  return (
    <div className="page">
      <section className="panel detail" aria-label="C-Eval 结果">
        <div className="panel-head"><h2>结果</h2><BackLink /></div>
        {!run && <p className="hint">读取中或不存在。</p>}
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
            <p className="hint">
              指标分两栏：native.*（Runner 原始聚合）与 diagnostic.*（平台重算）分开保存与显示，互不覆盖。
            </p>
          </>
        )}
      </section>
    </div>
  );
}

/** 比较页：逐条件原因；缺覆盖/不可比不显示放行。 */
export function CevalCompare() {
  const [params] = useSearchParams();
  const initial = (params.get("runs") ?? "").split(",").filter(Boolean);
  const [baseline, setBaseline] = useState(initial[0] ?? "");
  const [candidate, setCandidate] = useState(initial[1] ?? "");
  const [comparison, setComparison] = useState<{ eligible: boolean; reasons: string[]; case_diff: any } | null>(null);
  const [gate, setGate] = useState<{ passed: boolean; rules: { id: string; passed: boolean; reason: string }[] } | null>(null);
  const [error, setError] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setComparison(null);
    setGate(null);
    compareRuns(baseline, candidate)
      .then((payload) => {
        setComparison(payload);
        return evaluateRunGate({
          run_id: candidate,
          baseline_run_id: baseline,
          policy: {
            metric: "accuracy", op: "gte", threshold: 0.5,
            required_coverage: 1.0, require_cost_known: true, require_comparable: true,
          },
        });
      })
      .then(setGate)
      .catch((e) => setError(String(e)));
  };

  return (
    <div className="page">
      <section className="panel detail" aria-label="C-Eval 比较">
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
