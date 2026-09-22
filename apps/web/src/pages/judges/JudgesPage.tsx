import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import * as Switch from "@radix-ui/react-switch";
import {
  ArrowClockwiseIcon,
  CurrencyDollarIcon,
  GavelIcon,
  PlusIcon,
  ProhibitIcon,
  SealCheckIcon,
} from "@phosphor-icons/react";
import {
  cancelJudgeJob,
  describeApiError,
  getJudgeJob,
  getJudges,
  getReport,
  getScoringPasses,
  preflightJudge,
  submitJudgeJob,
  type JudgeCalibrationView,
  type JudgeJobRecord,
  type JudgePreflightRequest,
  type JudgePreflightView,
  type JudgeSpecView,
} from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import {
  CapabilityNotice,
  CostView,
  LoadFailure,
  UNKNOWN_TEXT,
  UnknownValue,
  failureText,
  unavailableReason,
} from "../../components/capability";
import { displayValue } from "../../components/jsonValues";

/**
 * Judge 管理视图：证据、rubric、校准状态（experimental vs calibrated）与独立成本，
 * 以及需要显式授权的付费提交。读取历史 / 切换 pass / 刷新只走 GET，永不收费。
 */

/** 校准状态：缺字段 = 未运行；未知取值原样显示（不按已校准处理）。 */
export function calibrationStatusOf(calibration: JudgeCalibrationView | null | undefined): string {
  const status = calibration?.status;
  if (typeof status === "string" && status.trim() !== "") return status;
  return "not_run";
}

/** 校准状态的资格含义：只有 calibrated 才可用于正式阻断 Gate。 */
export function calibrationGateText(status: string): string {
  if (status === "calibrated") return "已校准：可用于正式阻断 Gate（阈值由该 rubric 的校准政策固定）";
  if (status === "experimental") return "实验性：未通过校准阈值，不进入正式阻断 Gate";
  if (status === "not_run") return "未运行：没有校准证据，不构成已校准声明";
  if (status === "unavailable") return "能力不可用：服务端未提供校准报告";
  return "未知校准状态：不得按已校准使用";
}

/** 预检给出的费用覆盖：已知 / 未知必须分开说，未知不得伪装成硬上限。 */
export function preflightCostSummary(preflight: JudgePreflightView | null): string {
  if (!preflight) return "未预检";
  const coverage = preflight.price_coverage;
  if (!coverage) return UNKNOWN_TEXT + "（服务端未给出费用覆盖）";
  if (coverage.known === true) {
    const estimated = coverage.estimated_cost_usd;
    if (estimated == null) return "价格表已知，但服务端未给出估算金额";
    return "估计 $" + estimated + (coverage.price_table_version ? "（价格表 " + coverage.price_table_version + "）" : "");
  }
  return "未知：价格表缺失，不能声称存在精确货币硬上限";
}

function JudgeCriteria({ judge }: { judge: JudgeSpecView }) {
  const rubric = judge.rubric;
  if (!rubric) return <UnknownValue reason="服务端未登记 rubric" />;
  return (
    <>
      <dl className="kv">
        <dt>rubric</dt>
        <dd className="mono">{rubric.rubric_id + "@" + rubric.version}</dd>
        <dt>rubric hash</dt>
        <dd className="mono">{rubric.content_sha256 ?? UNKNOWN_TEXT}</dd>
        <dt>量表</dt>
        <dd className="mono">{rubric.scale ?? UNKNOWN_TEXT}</dd>
        <dt>缺证据政策</dt>
        <dd className="mono">{rubric.missing_evidence_policy ?? UNKNOWN_TEXT}</dd>
      </dl>
      <table>
        <thead><tr><th>criterion</th><th>说明</th><th>量表</th><th>权重</th></tr></thead>
        <tbody>
          {(rubric.criteria ?? []).map((criterion, index) => {
            const criterionId = criterion.id ?? criterion.criterion_id ?? `criterion-${index + 1}`;
            return (
            <tr key={criterionId}>
              <td className="mono nowrap">{criterionId}</td>
              <td>{criterion.description ?? "—"}</td>
              <td className="mono">{criterion.scale ?? UNKNOWN_TEXT}</td>
              <td className="mono">{criterion.weight ?? UNKNOWN_TEXT}</td>
            </tr>
            );
          })}
          {(rubric.criteria ?? []).length === 0 && (
            <tr><td colSpan={4} className="empty">服务端未登记逐项 criterion</td></tr>
          )}
        </tbody>
      </table>
    </>
  );
}

function JudgeCalibration({ judge }: { judge: JudgeSpecView }) {
  const calibration = judge.calibration ?? null;
  const status = calibrationStatusOf(calibration);
  return (
    <>
      <p data-testid="judge-calibration-status">
        <StatusBadge status={status} />
        <span className="hint"> {calibrationGateText(status)}</span>
      </p>
      <dl className="kv">
        <dt>校准集版本</dt>
        <dd className="mono">{calibration?.calibration_version ?? UNKNOWN_TEXT}</dd>
        <dt>样本数</dt>
        <dd className="mono">{calibration?.samples ?? UNKNOWN_TEXT}</dd>
        <dt>人工复核样本</dt>
        <dd className="mono">
          {calibration?.human_reviewed ?? UNKNOWN_TEXT}
          {calibration?.required_samples != null ? " / 要求 " + calibration.required_samples : ""}
        </dd>
        <dt>校准时间</dt>
        <dd className="mono">{calibration?.calibrated_at ?? UNKNOWN_TEXT}</dd>
      </dl>
      {calibration?.synthetic_only === true && (
        <CapabilityNotice testId="judge-synthetic-only">
          当前校准只由合成 fixture 得到标签，不构成人类质量真值；合成标签不满足人审门。
        </CapabilityNotice>
      )}
      {(calibration?.reasons ?? []).length > 0 && (
        <ul className="failure-list">
          {(calibration?.reasons ?? []).map((reason) => <li key={reason} className="hint fail">{reason}</li>)}
        </ul>
      )}
      <table>
        <thead><tr><th>criterion</th><th>指标</th><th>取值</th><th>口径</th></tr></thead>
        <tbody>
          {(calibration?.disagreements ?? []).map((row, index) => (
            <tr key={(row.criterion ?? row.metric ?? "row") + "-" + index}>
              <td className="mono nowrap">{row.criterion ?? UNKNOWN_TEXT}</td>
              <td className="mono">{row.metric ?? UNKNOWN_TEXT}</td>
              <td className="mono">{row.value ?? UNKNOWN_TEXT}</td>
              <td>{row.basis ?? "—"}</td>
            </tr>
          ))}
          {(calibration?.disagreements ?? []).length === 0 && (
            <tr><td colSpan={4} className="empty">暂无逐 criterion 分歧数据</td></tr>
          )}
        </tbody>
      </table>
      <dl className="kv">
        <dt>位置交换</dt>
        <dd className="mono">{calibration?.position_swap ? displayValue(calibration.position_swap) : UNKNOWN_TEXT}</dd>
        <dt>重复稳定性</dt>
        <dd className="mono">{calibration?.repeat_stability ? displayValue(calibration.repeat_stability) : UNKNOWN_TEXT}</dd>
        <dt>政策阈值</dt>
        <dd className="mono">{calibration?.policy ? displayValue(calibration.policy) : UNKNOWN_TEXT}</dd>
      </dl>
    </>
  );
}

const TERMINAL_JUDGE_JOB_STATUSES = new Set(["settled", "failed", "cancelled", "indeterminate"]);

function JudgeSubmitForm({ judge }: { judge: JudgeSpecView }) {
  const modes = useMemo(() => (judge.modes ?? []).filter((mode) => typeof mode === "string" && mode !== ""), [judge.modes]);
  const [purpose, setPurpose] = useState(modes[0] ?? "");
  const [runId, setRunId] = useState("");
  const [sourcePass, setSourcePass] = useState("");
  const [repeats, setRepeats] = useState("1");
  const [requestKey, setRequestKey] = useState("");
  const [preflight, setPreflight] = useState<JudgePreflightView | null>(null);
  const [preflightFor, setPreflightFor] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState<"preflight" | "submit" | "cancel" | null>(null);
  const [error, setError] = useState("");
  const [job, setJob] = useState<JudgeJobRecord | null>(null);
  const [submitUnknown, setSubmitUnknown] = useState("");
  const submitSeq = useRef(0);

  const repeatCount = Number(repeats);
  const formKey = [judge.judge_id, judge.version, purpose, runId, sourcePass, repeats].join("|");
  const preflightFresh = preflightFor === formKey && preflight !== null;
  const previewMaxCalls = judge.budget?.max_calls ?? judge.spec?.budget?.max_calls ?? 0;
  const previewTokensPerCall =
    (judge.spec?.budget?.max_prompt_tokens ?? 0) +
    (judge.spec?.budget?.max_completion_tokens ?? 0);
  const previewMaxTotalTokens =
    previewMaxCalls > 0 && previewTokensPerCall > 0
      ? previewMaxCalls * previewTokensPerCall
      : null;

  const request: JudgePreflightRequest | null = judge.spec ? {
    run_id: runId.trim(),
    mode: purpose === "pairwise" ? "pairwise" : "single",
    spec: judge.spec,
    source_pass_id: sourcePass.trim() === "" ? undefined : sourcePass.trim(),
    case_ids: [],
    repeats: Number.isFinite(repeatCount) && repeatCount > 0 ? repeatCount : 1,
    authorisation: {
      authorised: true,
      actor: "web-operator-preflight",
      max_calls: previewMaxCalls,
      max_total_tokens: previewMaxTotalTokens,
      hard_cost_cap_usd: judge.budget?.hard_cost_cap_usd ?? null,
    },
    publish_policy: "all_scored",
  } : null;

  const onPreflight = async () => {
    const seq = ++submitSeq.current;
    setBusy("preflight");
    setError("");
    if (request === null || request.run_id === "") return;
    try {
      const result = await preflightJudge(request);
      if (submitSeq.current !== seq) return; // 表单已改动：迟到预检不得当成当前结论
      setPreflight(result);
      setPreflightFor(formKey);
      setRequestKey(`judge-web-${crypto.randomUUID()}`);
      setConfirmed(false);
    } catch (caught) {
      if (submitSeq.current !== seq) return;
      setPreflight(null);
      setPreflightFor(null);
      setError(failureText(caught, "预检"));
    } finally {
      if (submitSeq.current === seq) setBusy(null);
    }
  };

  const blockedReason =
    modes.length === 0 ? "服务端未声明可用于付费执行的用途（modes）：提交入口禁用"
      : judge.spec == null ? "服务端未返回可执行 Judge spec"
        : purpose === "" ? "未选择用途"
          : runId.trim() === "" ? "必须填写被评 Run"
            : job !== null ? "本次请求已提交，等待作业终态；再次提交请重新读取预检"
              : !preflightFresh ? "必须先读取只读预检（预检本身不产生任何调用）"
                : preflight?.budget_executable !== true ? "预检结论为预算不可执行：不会发出任何调用"
                  : !confirmed ? "需要显式确认用途、模型、证据范围与费用"
                    : requestKey === "" ? "缺少幂等请求键，请重新预检"
                      : null;
  const canSubmit = blockedReason === null && busy === null;
  const jobTerminal = job !== null && TERMINAL_JUDGE_JOB_STATUSES.has(job.status);

  const resetForNextRequest = () => {
    setJob(null);
    setPreflight(null);
    setPreflightFor(null);
    setConfirmed(false);
    setRequestKey("");
    setError("");
    setSubmitUnknown("");
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit || preflight === null || request === null) return;
    setBusy("submit");
    setError("");
    setSubmitUnknown("");
    try {
      const record = await submitJudgeJob({
        ...request,
        request_key: requestKey,
        authorisation: {
          authorised: true,
          actor: "web-operator",
          max_calls: preflight.max_calls ?? 0,
          max_total_tokens: preflight.token_ceiling?.total ?? null,
          hard_cost_cap_usd: judge.budget?.hard_cost_cap_usd ?? null,
        },
      });
      setJob(record);
      setPreflight(null);
      setPreflightFor(null);
      setConfirmed(false);
    } catch (caught) {
      // 提交失败保留全部表单内容；结果未知时不自动重试（避免重复付费）。
      // 重新提交必须由操作员再次显式确认，不能沿用上一次的勾选。
      setError(failureText(caught, "提交"));
      setSubmitUnknown("提交结果未知：请先用幂等键核对作业状态；再次提交需要重新确认，绝不自动重试。");
      setConfirmed(false);
    } finally {
      setBusy(null);
    }
  };

  const onCancel = async () => {
    if (!job) return;
    setBusy("cancel");
    setError("");
    try {
      setJob(await cancelJudgeJob(job.job_id));
    } catch (caught) {
      setError(failureText(caught, "取消作业"));
    } finally {
      setBusy(null);
    }
  };

  const refreshJob = async () => {
    if (!job) return;
    try {
      setJob(await getJudgeJob(job.job_id));
    } catch (caught) {
      setError(failureText(caught, "刷新作业"));
    }
  };

  return (
    <form onSubmit={onSubmit} aria-label="Judge 付费提交">
      <h3 className="embed-title">付费提交（Judge 调用）</h3>
      <label htmlFor="judge-purpose">
        用途
        <select
          id="judge-purpose"
          className="control"
          value={purpose}
          disabled={modes.length === 0}
          onChange={(change) => { setPurpose(change.target.value); setPreflight(null); setPreflightFor(null); setConfirmed(false); }}
        >
          {modes.length === 0
            ? <option value="">（服务端未声明用途）</option>
            : modes.map((mode) => <option key={mode} value={mode}>{mode}</option>)}
        </select>
      </label>
      <label htmlFor="judge-run">
        被评 Run
        <input
          id="judge-run"
          className="mono"
          value={runId}
          placeholder="run id"
          onChange={(change) => { setRunId(change.target.value); setPreflightFor(null); setConfirmed(false); }}
        />
      </label>
      <label htmlFor="judge-pass">
        来源 pass（可选）
        <input
          id="judge-pass"
          className="mono"
          value={sourcePass}
          placeholder="scoring pass id"
          onChange={(change) => { setSourcePass(change.target.value); setPreflightFor(null); setConfirmed(false); }}
        />
      </label>
      <label htmlFor="judge-repeats">
        重复次数（含换序计量）
        <input
          id="judge-repeats"
          type="number"
          min={1}
          value={repeats}
          onChange={(change) => { setRepeats(change.target.value); setPreflightFor(null); setConfirmed(false); }}
        />
      </label>

      <dl className="kv" data-testid="judge-paid-summary">
        <dt>用途</dt>
        <dd className="mono">{purpose === "" ? "未选择" : purpose}</dd>
        <dt>模型</dt>
        <dd className="mono">{judge.model ?? UNKNOWN_TEXT}</dd>
        <dt>证据样本数</dt>
        <dd className="mono">{preflightFresh ? (preflight?.sample_count ?? UNKNOWN_TEXT) : "由服务端预检"}</dd>
        <dt>最大调用次数</dt>
        <dd className="mono">{preflightFresh ? (preflight?.max_calls ?? UNKNOWN_TEXT) : "未预检"}</dd>
        <dt>已知 / 未知费用</dt>
        <dd data-testid="judge-cost-coverage">{preflightCostSummary(preflightFresh ? preflight : null)}</dd>
        <dt>Judge 预算</dt>
        <dd className="mono">
          max_calls={judge.budget?.max_calls ?? UNKNOWN_TEXT} hard_cost_cap_usd={judge.budget?.hard_cost_cap_usd ?? UNKNOWN_TEXT}
        </dd>
      </dl>

      <div className="actions">
        <button type="button" disabled={busy !== null || modes.length === 0 || request === null || request.run_id === ""} onClick={() => void onPreflight()}>
          {busy === "preflight" ? "预检中…" : "读取预检（只读，零调用）"}
        </button>
        <button
          type="submit"
          className="primary"
          disabled={!canSubmit}
          title={blockedReason ?? undefined}
          data-testid="judge-submit-button"
        >
          {busy === "submit" ? "提交中…" : "提交 Judge 评分（会产生调用与费用）"}
        </button>
      </div>
      {blockedReason && (
        <p className="hint fail" data-testid="judge-submit-reason">{blockedReason}</p>
      )}

      <div className="switch-row">
        <span className="field-label">我已确认用途、模型、证据范围与费用（提交会产生真实调用与费用）</span>
        <Switch.Root
          className="switch"
          checked={confirmed}
          disabled={!preflightFresh}
          onCheckedChange={setConfirmed}
          aria-label="确认付费提交"
        >
          <Switch.Thumb className="switch-thumb" />
        </Switch.Root>
      </div>

      {preflightFresh && (preflight?.reasons ?? []).length > 0 && (
        <ul className="failure-list" data-testid="judge-preflight-reasons">
          {(preflight?.reasons ?? []).map((reason) => <li key={reason} className="hint">{reason}</li>)}
        </ul>
      )}
      {preflightFresh && preflight?.hard_monetary_cap !== true && (
        <CapabilityNotice testId="judge-hard-cap-unknown">
          预检没有给出可证明的货币硬上限：未知价格不得声称精确预算上限，调用次数上限仍由 max_calls 约束。
        </CapabilityNotice>
      )}
      {error && <p className="error" role="alert" data-testid="judge-submit-error">{error}</p>}
      {submitUnknown && <CapabilityNotice testId="judge-submit-unknown">{submitUnknown}</CapabilityNotice>}

      {job && (
        <dl className="kv" data-testid="judge-job">
          <dt>作业</dt>
          <dd className="mono">{job.job_id}</dd>
          <dt>状态</dt>
          <dd><StatusBadge status={job.status} /></dd>
          <dt>调用</dt>
          <dd className="mono">{(job.calls_made ?? UNKNOWN_TEXT) + " / " + (job.max_calls ?? preflight?.max_calls ?? UNKNOWN_TEXT)}</dd>
          <dt>评分批次</dt>
          <dd className="mono">{job.scoring_pass_id ?? UNKNOWN_TEXT}</dd>
          <dt>取消</dt>
          <dd className="mono">{job.cancellation ? (job.cancellation.state ?? UNKNOWN_TEXT) + "（重复取消返回同一状态）" : "未请求"}</dd>
        </dl>
      )}
      {job && (
        <div className="actions">
          <button type="button" disabled={busy !== null || jobTerminal} onClick={() => void onCancel()}>
            <ProhibitIcon size={14} weight="bold" aria-hidden /> 取消该作业
          </button>
          <button type="button" disabled={busy !== null} onClick={() => void refreshJob()}>
            <ArrowClockwiseIcon size={14} weight="bold" aria-hidden /> 刷新作业状态
          </button>
          {jobTerminal && (
            <button type="button" disabled={busy !== null} onClick={resetForNextRequest}>
              <PlusIcon size={14} weight="bold" aria-hidden /> 新建 Judge 请求
            </button>
          )}
          <span className="hint">取消只作用于该作业；已完成的历史评分批次保持不变。</span>
        </div>
      )}
      {job?.cost && <CostView cost={job.cost} testId="judge-job-cost" />}
    </form>
  );
}

interface ScoreRow {
  case_id: string;
  metric_id: string;
  metric_status: string;
  passed: boolean | null;
  reason: string | null;
}

function JudgeHistory() {
  const [runId, setRunId] = useState("");
  const [passes, setPasses] = useState<Array<Record<string, any>> | null>(null);
  const [selectedPass, setSelectedPass] = useState("");
  const [scores, setScores] = useState<ScoreRow[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  /** 迟到响应不得覆盖新选择的 pass。 */
  const passSeq = useRef(0);

  const loadPasses = useCallback(async () => {
    const id = runId.trim();
    if (id === "") return;
    const seq = ++passSeq.current;
    setBusy(true);
    setError("");
    try {
      const payload = await getScoringPasses(id);
      if (passSeq.current !== seq) return;
      setPasses(payload.items);
      setSelectedPass("");
      setScores(null);
    } catch (caught) {
      if (passSeq.current !== seq) return;
      setPasses(null);
      setError(failureText(caught, "读取历史"));
    } finally {
      if (passSeq.current === seq) setBusy(false);
    }
  }, [runId]);

  const selectPass = async (passId: string) => {
    const seq = ++passSeq.current;
    setSelectedPass(passId);
    setScores(null);
    if (passId === "") return;
    try {
      const report = await getReport(runId.trim(), passId);
      if (passSeq.current !== seq) return; // 已切换到其它 pass：迟到报告丢弃
      setScores(((report.scores as any[]) ?? [])
        .filter((score) => typeof score.metric_id === "string")
        .map((score) => ({
          case_id: String(score.case_id ?? UNKNOWN_TEXT),
          metric_id: String(score.metric_id),
          metric_status: String(score.metric_status ?? "scored"),
          passed: score.passed ?? null,
          reason: score.reason ?? null,
        })));
    } catch (caught) {
      if (passSeq.current !== seq) return; // 迟到的失败同样不得覆盖
      setError(failureText(caught, "读取该 pass"));
    }
  };

  return (
    <section className="panel detail" aria-label="评分历史（只读）">
      <div className="panel-head">
        <h2>评分历史（只读）</h2>
      </div>
      <CapabilityNotice tone="info" testId="judge-history-readonly">
        读取历史、切换 pass 与刷新只调用 GET 接口：不会调用 Judge，也不产生任何费用。
      </CapabilityNotice>
      <div className="inline-field">
        <label className="field-label" htmlFor="judge-history-run">Run</label>
        <input
          id="judge-history-run"
          className="control mono"
          value={runId}
          placeholder="run id"
          onChange={(change) => setRunId(change.target.value)}
        />
        <button type="button" disabled={busy || runId.trim() === ""} onClick={() => void loadPasses()}>
          读取历史
        </button>
      </div>
      {error && <p className="error" role="alert" data-testid="judge-history-error">{error}</p>}
      {passes && (
        <table>
          <thead><tr><th>pass</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody>
            {passes.map((pass) => {
              const passId = String(pass.id ?? pass.scoring_pass_id ?? "");
              return (
                <tr key={passId}>
                  <td className="mono nowrap">{passId}</td>
                  <td>{pass.status ? <StatusBadge status={String(pass.status)} /> : <UnknownValue reason="服务端未给出批次状态" />}</td>
                  <td className="mono nowrap">{pass.created_at ?? UNKNOWN_TEXT}</td>
                  <td className="row-actions">
                    <button type="button" className="link" disabled={passId === ""} onClick={() => void selectPass(passId)}>
                      查看（只读）
                    </button>
                  </td>
                </tr>
              );
            })}
            {passes.length === 0 && <tr><td colSpan={4} className="empty">该运行没有评分批次</td></tr>}
          </tbody>
        </table>
      )}
      {selectedPass && (
        <>
          <h3 className="embed-title">pass {selectedPass} 的分数（只读）</h3>
          <table data-testid="judge-history-scores">
            <thead><tr><th>case</th><th>metric</th><th>状态</th><th>判定</th><th>理由</th></tr></thead>
            <tbody>
              {(scores ?? []).map((score, index) => (
                <tr key={score.case_id + "-" + score.metric_id + "-" + index}>
                  <td className="mono nowrap">{score.case_id}</td>
                  <td className="mono nowrap">{score.metric_id}</td>
                  <td className="mono">{score.metric_status}</td>
                  <td>
                    {score.passed === true
                      ? <span className="pass">通过</span>
                      : score.passed === false
                        ? <span className="fail">未通过</span>
                        : <UnknownValue reason="没有判定（insufficient / error 等）" />}
                  </td>
                  <td>{score.reason ?? "—"}</td>
                </tr>
              ))}
              {scores === null && <tr><td colSpan={5} className="empty">加载中…</td></tr>}
              {scores !== null && scores.length === 0 && <tr><td colSpan={5} className="empty">该批次没有可显示的分数</td></tr>}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

export function JudgesPage() {
  const [judges, setJudges] = useState<JudgeSpecView[]>([]);
  const [listError, setListError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoaded(false);
    try {
      const payload = await getJudges();
      setJudges(payload.items);
      setListError(null);
    } catch (error) {
      setJudges([]);
      setListError(error);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const judge = useMemo(() => {
    if (judges.length === 0) return null;
    return judges.find((item) => item.judge_id + "@" + item.version === selected) ?? judges[0];
  }, [judges, selected]);

  const calibrationStatus = judge ? calibrationStatusOf(judge.calibration) : "not_run";

  return (
    <div className="page">
      <aside className="panel list-panel" aria-label="Judge 清单">
        <div className="panel-head">
          <h2>Judge</h2>
          <div className="panel-head-actions">
            <button type="button" className="icon-btn" aria-label="刷新清单" onClick={() => void refresh()}>
              <ArrowClockwiseIcon size={14} weight="bold" aria-hidden />
            </button>
          </div>
        </div>
        {listError !== null && <LoadFailure error={listError} what="Judge 清单" testId="judge-list-unavailable" />}
        {!loaded && judges.length === 0 ? (
          <p className="empty">加载中</p>
        ) : judges.length === 0 ? (
          <p className="empty">暂无已发布 Judge</p>
        ) : (
          <ul className="provider-list">
            {judges.map((item) => {
              const key = item.judge_id + "@" + item.version;
              const status = calibrationStatusOf(item.calibration);
              return (
                <li key={key}>
                  <button
                    type="button"
                    className="provider-item"
                    data-state={judge && judge.judge_id + "@" + judge.version === key ? "active" : undefined}
                    onClick={() => setSelected(key)}
                  >
                    <GavelIcon size={16} weight="bold" aria-hidden />
                    <span className="provider-item-name mono">{key}</span>
                    <StatusBadge status={status} />
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </aside>

      <section className="panel wide-panel" aria-label="Judge 详情">
        <div className="panel-head">
          <h2>Judge 详情</h2>
          {judge && <span className="hint mono">{judge.model ?? UNKNOWN_TEXT}</span>}
        </div>
        {!judge ? (
          <p className="empty">选择左侧（或先发布）一个 Judge。</p>
        ) : (
          <>
            <dl className="kv">
              <dt>Judge</dt>
              <dd className="mono">{judge.judge_id + "@" + judge.version}</dd>
              <dt>spec hash</dt>
              <dd className="mono">{judge.spec_sha256 ?? UNKNOWN_TEXT}</dd>
              <dt>Provider / 模型</dt>
              <dd className="mono">{(judge.provider ?? UNKNOWN_TEXT) + " / " + (judge.model ?? UNKNOWN_TEXT)}</dd>
              <dt>用途清单</dt>
              <dd className="mono">{(judge.modes ?? []).length === 0 ? UNKNOWN_TEXT : (judge.modes ?? []).join(", ")}</dd>
            </dl>

            <h3 className="embed-title">证据</h3>
            <dl className="kv">
              <dt>证据选择</dt>
              <dd className="mono">{judge.evidence?.selector ? displayValue(judge.evidence.selector) : UNKNOWN_TEXT}</dd>
              <dt>样本范围</dt>
              <dd className="mono">{(judge.evidence?.case_ids ?? []).length === 0 ? UNKNOWN_TEXT : (judge.evidence?.case_ids ?? []).join(", ")}</dd>
              <dt>观察引用</dt>
              <dd className="mono">{(judge.evidence?.observation_refs ?? []).join(", ") || UNKNOWN_TEXT}</dd>
              <dt>缺证据政策</dt>
              <dd className="mono">{judge.evidence?.missing_policy ?? UNKNOWN_TEXT}</dd>
            </dl>
            <CapabilityNotice tone="info">
              证据引用不属于该 Observation 时不得作为有效评分依据；理由不等于证据。
            </CapabilityNotice>

            <h3 className="embed-title">Rubric</h3>
            <JudgeCriteria judge={judge} />

            <h3 className="embed-title">校准状态</h3>
            <JudgeCalibration judge={judge} />

            <h3 className="embed-title">独立成本</h3>
            <CostView cost={judge.cost} testId="judge-independent-cost" />
            <p className="hint">
              Judge 调用与 subject 调用分开计量；同一 token 不会同时计入 Skill 开销与模型费用。
            </p>
          </>
        )}
      </section>

      {judge && (
        <section className="panel form-panel" aria-label="Judge 付费提交">
          <JudgeSubmitForm key={judge.judge_id + "@" + judge.version} judge={judge} />
        </section>
      )}

      {judge && calibrationStatus !== "calibrated" && (
        <section className="panel form-panel">
          <h2>Gate 资格</h2>
          <p className="hint" data-testid="judge-gate-eligibility">{calibrationGateText(calibrationStatus)}</p>
          <p className="hint">
            <SealCheckIcon size={14} weight="bold" aria-hidden /> 未经校准的 Judge 只用于实验性评分；人工修订会追加新 pass，不改历史分数。
          </p>
        </section>
      )}

      <JudgeHistory />

      <section className="panel detail">
        <h2>成本与授权边界</h2>
        <p className="hint">
          <CurrencyDollarIcon size={14} weight="bold" aria-hidden /> GET 报告、读取校准、切换 pass 与刷新都不会触发 Judge；
          付费提交只在操作员显式确认后发生，并按幂等键避免重复收费。
        </p>
        <p className="hint">
          <ProhibitIcon size={14} weight="bold" aria-hidden /> 缺少用途 / 预检 / 授权 / 预算结论时提交入口禁用并给出原因，不静默隐藏。
        </p>
      </section>
    </div>
  );
}
