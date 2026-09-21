import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ArrowClockwiseIcon, FlaskIcon, MagicWandIcon, TestTubeIcon } from "@phosphor-icons/react";
import {
  compareRuns,
  describeApiError,
  getReport,
  getRun,
  getSkills,
  modelLabel,
  testSkillBehaviour,
  testSkillFixture,
  validateSkill,
  validationIssuesFromError,
  type ComparisonReportView,
  type RunRecord,
  type SkillValidationResult,
  type SkillVerificationScopeView,
  type SkillVersionRecord,
  type ValidationIssue,
} from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import {
  CapabilityNotice,
  LoadFailure,
  UNKNOWN_TEXT,
  UnknownValue,
  failureText,
  unavailableReason,
  type CostLike,
} from "../../components/capability";
import { displayValue, numberAt, readPath } from "../../components/jsonValues";

/**
 * Skill 页面：三种验证范围必须分开呈现（静态校验 / executable fixture /
 * 固定 Agent 行为测试），以及 no-skill vs skill-v1 vs skill-v2 的三臂对照。
 * 能力缺失时入口禁用并给出原因，不用「已验证」合并三种范围。
 */

export interface ScopeGateInputs {
  agentId: string;
  model: string;
}

export interface ValidationScopeDefinition {
  id: "static" | "executable_fixture" | "behaviour";
  label: string;
  proves: string;
  cannot: string;
  /** null = 可运行；字符串 = 禁用原因（必须显示，不能隐藏按钮）。 */
  gate: (skill: SkillVersionRecord, inputs: ScopeGateInputs) => string | null;
}

export const VALIDATION_SCOPES: ValidationScopeDefinition[] = [
  {
    id: "static",
    label: "静态校验",
    proves: "manifest、资源哈希、schema、依赖固定与权限声明有效",
    cannot: "指令能完成业务任务",
    gate: () => null,
  },
  {
    id: "executable_fixture",
    label: "executable fixture",
    proves: "指定入口在受控输入下的输出与副作用符合约束",
    cannot: "所有模型都会正确使用它",
    gate: (skill) => (skill.kind === "executable"
      ? null
      : "该 Skill 不是 executable（kind=" + displayValue(skill.kind) + "）：没有受控可执行入口，不能用读取指令文件冒充已执行；它的独立验证是静态校验或固定 Agent 行为测试。"),
  },
  {
    id: "behaviour",
    label: "固定 Agent 行为测试",
    proves: "固定 Agent / 模型 / 任务下选择与使用 Skill 的实际效果",
    cannot: "Skill 对任意模型或任务都有效",
    gate: (_skill, inputs) => (inputs.agentId.trim() === "" || inputs.model.trim() === ""
      ? "必须显式指定 Agent 与模型：条件未固定时行为测试不可比较"
      : null),
  },
];

/** 服务端未声明 kind 时是未知，不推断成 executable。 */
export function skillKindOf(skill: SkillVersionRecord): string {
  return typeof skill.kind === "string" && skill.kind.trim() !== "" ? skill.kind : "unknown";
}

export function skillIdentityOf(skill: SkillVersionRecord): string {
  const identity = skill.skill_id ?? skill.name;
  return typeof identity === "string" && identity.trim() !== "" ? identity : UNKNOWN_TEXT;
}

/** 验证范围状态：服务端未登记时按原样显示（statusLabel 回退），缺字段 = 未运行。 */
export function scopeStatusOf(scope: SkillVerificationScopeView | null | undefined): string {
  const status = scope?.status;
  if (typeof status === "string" && status.trim() !== "") return status;
  return "not_run";
}

/**
 * 静态校验响应 → 验证范围视图。服务端把作用域边界写在响应里：
 * validation_scope 只有 static、resource_bytes_verified=false（资源字节核验属于
 * executable fixture 作用域），页面照实显示为「未知/不适用」而不是通过。
 */
export function staticScopeFromValidation(result: SkillValidationResult): SkillVerificationScopeView {
  const dependencies = result.dependency_refs ?? [];
  const paths = result.resource_paths ?? [];
  return {
    status: result.ok === true ? "passed" : "failed",
    checks: [
      {
        id: "contract",
        locator: "manifest",
        passed: result.ok === true,
        message: result.ok === true ? "契约、kind 分型与 schema 有效" : "服务端拒绝该文档",
      },
      {
        id: "dependency_pin",
        locator: "dependency_refs",
        passed: true,
        message: dependencies.length + " 条依赖引用已核验（版本固定）",
      },
      {
        id: "resource_manifest",
        locator: "resource_manifest",
        passed: paths.length > 0 ? true : null,
        message: paths.length > 0
          ? paths.length + " 个资源路径已登记（静态范围不读取字节）"
          : "没有登记资源路径",
      },
      {
        id: "resource_bytes",
        locator: "resource_manifest",
        passed: null,
        message: result.resource_bytes_verified === false
          ? "静态范围不核验资源字节：属于 executable fixture 作用域（M5-A10）"
          : "服务端声明资源字节已核验",
      },
      {
        id: "entrypoint",
        locator: "entrypoint",
        passed: result.executable === true,
        message: result.executable === true ? "已声明受控可执行入口" : "无可执行入口（纯指令 / 带资源 Skill）",
      },
    ],
    conditions: {
      validation_scope: result.validation_scope ?? "unknown",
      resource_bytes_verified: result.resource_bytes_verified ?? false,
      content_hash: result.content_hash ?? "unknown",
      defaulted_fields: (result.defaulted_fields ?? []).join(", ") || "无",
    },
  };
}

/** 服务端 4xx 的逐字段拒绝 → 失败的静态校验结论（不是读取故障）。 */
export function staticScopeFromIssues(issues: ValidationIssue[]): SkillVerificationScopeView {
  return {
    status: "failed",
    checks: issues.map((issue) => ({
      id: issue.code,
      locator: issue.locator,
      passed: false,
      message: issue.message,
    })),
  };
}

function ScopeChecks({ scope }: { scope: SkillVerificationScopeView }) {
  const checks = scope.checks ?? [];
  if (checks.length === 0) return <p className="hint">服务端未返回逐项检查明细。</p>;
  return (
    <table>
      <thead>
        <tr><th>检查项</th><th>字段</th><th>结论</th><th>说明</th></tr>
      </thead>
      <tbody>
        {checks.map((check, index) => (
          <tr key={(check.id ?? check.locator ?? "check") + "-" + index}>
            <td className="mono nowrap">{check.id ?? UNKNOWN_TEXT}</td>
            <td className="mono nowrap">{check.locator ?? "—"}</td>
            <td>
              {check.passed === true
                ? <span className="pass">通过</span>
                : check.passed === false
                  ? <span className="fail">未通过</span>
                  : <UnknownValue reason="服务端没有给出该检查项的结论" />}
            </td>
            <td>{check.message ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function SkillValidationPage() {
  const [skills, setSkills] = useState<SkillVersionRecord[]>([]);
  const [listError, setListError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [agentId, setAgentId] = useState("");
  const [model, setModel] = useState("");
  const [results, setResults] = useState<Record<string, SkillVerificationScopeView | null>>({});
  const [unavailable, setUnavailable] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState("");
  /** 迟到响应不得写到新选中的 Skill 上。 */
  const scopeSeq = useRef(0);

  const refresh = useCallback(async () => {
    setLoaded(false);
    try {
      const payload = await getSkills();
      setSkills(payload.items);
      setListError(null);
    } catch (error) {
      setSkills([]);
      setListError(error);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const skill = useMemo(() => {
    if (skills.length === 0) return null;
    return skills.find((item) => skillIdentityOf(item) + "@" + item.version === selected) ?? skills[0];
  }, [skills, selected]);

  const selectSkill = (item: SkillVersionRecord) => {
    scopeSeq.current += 1; // 切换 Skill：丢弃在途结果
    setSelected(skillIdentityOf(item) + "@" + item.version);
    setResults({});
    setUnavailable({});
    setActionError("");
  };

  const inputs: ScopeGateInputs = { agentId, model };

  const runScope = async (scope: ValidationScopeDefinition) => {
    if (!skill) return;
    const identity = skillIdentityOf(skill);
    if (identity === UNKNOWN_TEXT) {
      setActionError("服务端未给出 Skill 标识，无法发起校验");
      return;
    }
    const seq = scopeSeq.current;
    setBusy(scope.id);
    setActionError("");
    try {
      const result = scope.id === "static"
        // 静态校验把 Skill 文档本身交给服务端（只解析、不执行、零调用）
        ? staticScopeFromValidation(await validateSkill(skill as unknown as Record<string, unknown>))
        : scope.id === "executable_fixture"
          ? await testSkillFixture({ skill_id: identity, version: skill.version })
          : await testSkillBehaviour({ skill_id: identity, version: skill.version, agent_id: agentId, model });
      if (scopeSeq.current !== seq) return; // 已切换 Skill：迟到响应丢弃
      setResults((current) => ({ ...current, [scope.id]: result }));
    } catch (error) {
      if (scopeSeq.current !== seq) return;
      const issues = scope.id === "static" ? validationIssuesFromError(error, "manifest") : null;
      if (issues) {
        // 服务端 4xx 是静态校验结论：逐字段拒绝就地显示，不算能力不可用
        setResults((current) => ({ ...current, [scope.id]: staticScopeFromIssues(issues) }));
        return;
      }
      const reason = unavailableReason(error);
      if (reason) setUnavailable((current) => ({ ...current, [scope.id]: reason }));
      else setActionError(failureText(error, scope.label + "运行"));
    } finally {
      if (scopeSeq.current === seq) setBusy(null);
    }
  };

  const permissions = skill?.requested_permissions;

  return (
    <div className="page">
      <aside className="panel list-panel" aria-label="Skill 清单">
        <div className="panel-head">
          <h2>Skill</h2>
          <div className="panel-head-actions">
            <button type="button" className="icon-btn" aria-label="刷新清单" onClick={() => void refresh()}>
              <ArrowClockwiseIcon size={14} weight="bold" aria-hidden />
            </button>
          </div>
        </div>
        {listError !== null && <LoadFailure error={listError} what="Skill 清单" testId="skill-list-unavailable" />}
        {!loaded && skills.length === 0 ? (
          <p className="empty">加载中</p>
        ) : skills.length === 0 ? (
          <p className="empty">暂无已发布 Skill</p>
        ) : (
          <ul className="provider-list">
            {skills.map((item) => {
              const key = skillIdentityOf(item) + "@" + item.version;
              return (
                <li key={key}>
                  <button
                    type="button"
                    className="provider-item"
                    data-state={skill && skillIdentityOf(skill) + "@" + skill.version === key ? "active" : undefined}
                    onClick={() => selectSkill(item)}
                  >
                    <MagicWandIcon size={16} weight="bold" aria-hidden />
                    <span className="provider-item-name mono">{key}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </aside>

      <section className="panel wide-panel" aria-label="Skill 验证范围">
        <div className="panel-head">
          <h2>Skill 验证范围</h2>
          {skill && <StatusBadge status={skillKindOf(skill)} />}
        </div>
        {!skill ? (
          <p className="empty">选择左侧（或先发布）一个 Skill 查看三种验证范围。</p>
        ) : (
          <>
            <dl className="kv">
              <dt>Skill</dt>
              <dd className="mono">{skillIdentityOf(skill) + "@" + skill.version}</dd>
              <dt>生命周期</dt>
              <dd className="mono">{skill.lifecycle ?? UNKNOWN_TEXT}</dd>
              <dt>内容 hash</dt>
              <dd className="mono">{skill.content_hash ?? UNKNOWN_TEXT}</dd>
              <dt>注入方式</dt>
              <dd className="mono">{skill.injection_mode ?? UNKNOWN_TEXT}</dd>
            </dl>

            <h3 className="embed-title">三种验证范围</h3>
            <p className="hint">
              三种范围能证明的东西不同，页面不把它们合并成一个「已验证」结论。
            </p>
            <table>
              <thead>
                <tr><th>验证范围</th><th>状态</th><th>能证明</th><th>不能证明</th><th>操作</th></tr>
              </thead>
              <tbody>
                {VALIDATION_SCOPES.map((scope) => {
                  const result = results[scope.id] ?? skill.verification?.[scope.id] ?? null;
                  const capabilityReason = unavailable[scope.id] ?? null;
                  const gateReason = scope.gate(skill, inputs);
                  const reason = capabilityReason
                    ? "能力不可用：" + capabilityReason
                    : gateReason;
                  const disabled = reason !== null || busy !== null;
                  return (
                    <tr key={scope.id}>
                      <td className="nowrap">
                        {scope.id === "static"
                          ? <FlaskIcon size={14} weight="bold" aria-hidden />
                          : scope.id === "executable_fixture"
                            ? <TestTubeIcon size={14} weight="bold" aria-hidden />
                            : <MagicWandIcon size={14} weight="bold" aria-hidden />}
                        {" "}{scope.label}
                      </td>
                      <td><StatusBadge status={scopeStatusOf(result)} /></td>
                      <td>{scope.proves}</td>
                      <td>{scope.cannot}</td>
                      <td className="row-actions">
                        <button
                          type="button"
                          className="link"
                          disabled={disabled}
                          title={reason ?? undefined}
                          data-testid={"scope-run-" + scope.id}
                          onClick={() => void runScope(scope)}
                        >
                          {busy === scope.id ? "运行中…" : "运行"}
                        </button>
                        {reason && (
                          <p className="hint fail" data-testid={"scope-reason-" + scope.id}>{reason}</p>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            <h3 className="embed-title">行为测试条件</h3>
            <div className="connection-grid">
              <label htmlFor="scope-agent">
                Agent
                <input
                  id="scope-agent"
                  value={agentId}
                  placeholder="例如 builtin-agent / claude-code"
                  onChange={(change) => setAgentId(change.target.value)}
                />
              </label>
              <label htmlFor="scope-model">
                模型
                <input
                  id="scope-model"
                  value={model}
                  placeholder="已发布的模型 id"
                  onChange={(change) => setModel(change.target.value)}
                />
              </label>
            </div>
            <p className="hint">行为测试只在 Agent / 模型 / 任务固定时有效；条件缺失时入口禁用并给出原因。</p>

            <h3 className="embed-title">验证结果</h3>
            {VALIDATION_SCOPES.map((scope) => {
              const result = results[scope.id] ?? skill.verification?.[scope.id] ?? null;
              if (!result) return null;
              return (
                <div key={scope.id}>
                  <p className="hint" data-testid={"scope-result-" + scope.id}>
                    <span className="field-label">{scope.label}</span>
                    <StatusBadge status={scopeStatusOf(result)} />
                    {result.ran_at && <span className="mono"> {result.ran_at}</span>}
                    {result.reason && <span className="hint fail"> {result.reason}</span>}
                  </p>
                  {result.run_id && (
                    <p className="hint">
                      引用既有 Run：
                      <Link className="link" to={"/scenario/runs/" + encodeURIComponent(result.run_id)}>
                        {result.run_id}
                      </Link>
                      {result.pass_id && <span className="mono"> pass={result.pass_id}</span>}
                    </p>
                  )}
                  {result.conditions && (
                    <dl className="kv">
                      {Object.entries(result.conditions).map(([key, value]) => (
                        <Fragment key={key}>
                          <dt>{key}</dt>
                          <dd className="mono">{displayValue(value)}</dd>
                        </Fragment>
                      ))}
                    </dl>
                  )}
                  <ScopeChecks scope={result} />
                </div>
              );
            })}
            {Object.values(results).every((value) => value == null) && !skill.verification && (
              <p className="empty">本会话尚未运行验证范围；服务端也没有登记历史结论（未运行不是通过）。</p>
            )}

            <h3 className="embed-title">资源、依赖与权限声明</h3>
            <table>
              <thead><tr><th>资源路径</th><th>sha256</th><th>大小</th><th>媒体类型</th></tr></thead>
              <tbody>
                {(skill.resource_manifest ?? []).map((resource) => (
                  <tr key={resource.path}>
                    <td className="mono">{resource.path}</td>
                    <td className="mono nowrap">{resource.sha256 ?? UNKNOWN_TEXT}</td>
                    <td className="mono nowrap">{resource.size_bytes ?? UNKNOWN_TEXT}</td>
                    <td className="mono">{resource.media_type ?? UNKNOWN_TEXT}</td>
                  </tr>
                ))}
                {(skill.resource_manifest ?? []).length === 0 && (
                  <tr><td colSpan={4} className="empty">该 Skill 没有声明资源</td></tr>
                )}
              </tbody>
            </table>
            <dl className="kv">
              <dt>入口</dt>
              <dd className="mono">
                {skill.entrypoint
                  ? displayValue([skill.entrypoint.interpreter, ...(skill.entrypoint.argv ?? [])])
                  : "无入口（纯指令或带资源 Skill）"}
              </dd>
              <dt>请求的权限</dt>
              <dd className="mono">{permissions ? displayValue(permissions) : "未声明权限"}</dd>
              <dt>依赖</dt>
              <dd className="mono">
                {(skill.dependency_refs ?? []).length === 0
                  ? "无依赖"
                  : (skill.dependency_refs ?? []).map((dependency) => displayValue(dependency.name) + "@" + displayValue(dependency.version)).join(", ")}
              </dd>
            </dl>
            <CapabilityNotice tone="info" testId="skill-permission-scope">
              权限声明不等于授权：最终生效权限是平台安全策略、Scenario 与请求权限的交集，Skill 不能把 deny 改成 real。
            </CapabilityNotice>
            {actionError && <p className="error" role="alert" data-testid="skill-action-error">{actionError}</p>}
          </>
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------- 三臂对照

export interface ArmView {
  runId: string;
  arm: string;
  model: string;
  skillRefs: string[];
  conditions: Record<string, string>;
  caseCount: number | null;
  passed: number | null;
  scored: number | null;
  skillTokenOverhead: number | null;
  toolCalls: number | null;
  cost: CostLike;
}

const ARM_CONDITIONS: Array<{ label: string; paths: string[] }> = [
  { label: "模型", paths: ["model", "provider.model"] },
  { label: "数据集", paths: ["dataset", "dataset_version", "benchmark_snapshot.dataset.name"] },
  { label: "样本选择", paths: ["case_selection.mode", "selection.mode"] },
  { label: "Agent / Runtime", paths: ["agent", "runtime", "execution.backend_id"] },
  { label: "工具权限", paths: ["tool_policy", "tools", "tool_modes"] },
  { label: "预算政策", paths: ["budget_policy", "budget.policy"] },
  { label: "评分器", paths: ["scoring.scorer_id", "scorer"] },
];

/** 三臂身份：manifest.skill_arm（no-skill / skill-v1 / skill-v2）；缺字段 = 未知。 */
export function skillArmOf(run: RunRecord): string {
  const arm = readPath(run.manifest, "skill_arm");
  return typeof arm === "string" && arm.trim() !== "" ? arm : UNKNOWN_TEXT;
}

export function skillRefsOf(run: RunRecord): string[] {
  const snapshot = readPath(run.manifest, "skill_snapshot");
  const refs = readPath(snapshot, "refs") ?? readPath(run.manifest, "skills");
  if (!Array.isArray(refs)) return [];
  return refs.map((item) => displayValue(item)).filter((item) => item !== UNKNOWN_TEXT);
}

/** Skill 注入的 token 开销：服务端未登记时未知，不按 0 计。 */
export function skillTokenOverheadOf(run: RunRecord): number | null {
  return numberAt(run.manifest, [
    "skill_token_overhead",
    "skill_injection.token_overhead",
    "skill_snapshot.token_overhead",
  ]);
}

/** 工具调用次数：逐 Case 汇总 result 里的登记值；任何一处都没有时未知。 */
export function toolCallCountOf(run: RunRecord): number | null {
  let total = 0;
  let found = false;
  for (const row of run.cases ?? []) {
    const value = numberAt(row.result, [
      "usage.tool_calls",
      "tool_calls",
      "tool_call_count",
      "agent.tool_calls",
    ]);
    if (value != null) {
      total += value;
      found = true;
    }
  }
  if (found) return total;
  return numberAt(run.manifest, ["tool_calls", "agent.tool_calls", "usage.tool_calls"]);
}

/** 三臂成本：报道成本来自该 pass 的报告；覆盖不完整时成本完整性仍是未知。 */
export function armCostOf(
  run: RunRecord,
  report: { cost?: { total?: number | null; known_cases?: number | null; unknown_cases?: number | null; price_table_versions?: string[] } } | null,
): CostLike {
  const total = report?.cost?.total ?? null;
  const unknownCases = report?.cost?.unknown_cases ?? null;
  return {
    reported_usd: total,
    estimated_usd: numberAt(run.manifest, ["estimated_cost_usd", "cost_estimate.estimated_usd"]),
    currency: "USD",
    known_calls: report?.cost?.known_cases ?? null,
    unknown_calls: unknownCases,
    unknown_cost: total == null || (unknownCases ?? 0) > 0,
    price_table_version: (report?.cost?.price_table_versions ?? [])[0] ?? null,
  };
}

function toArm(run: RunRecord, report: Parameters<typeof armCostOf>[1]): ArmView {
  const scores = run.scores ?? [];
  const conditions: Record<string, string> = {};
  for (const condition of ARM_CONDITIONS) {
    const value = condition.paths.map((path) => readPath(run.manifest, path)).find((item) => item !== undefined);
    conditions[condition.label] = displayValue(value);
  }
  return {
    runId: run.id,
    arm: skillArmOf(run),
    model: modelLabel(run) ?? UNKNOWN_TEXT,
    skillRefs: skillRefsOf(run),
    conditions,
    caseCount: run.case_ids ? run.case_ids.length : null,
    passed: scores.length > 0 ? scores.filter((score) => score.passed).length : null,
    scored: scores.length > 0 ? scores.length : null,
    skillTokenOverhead: skillTokenOverheadOf(run),
    toolCalls: toolCallCountOf(run),
    cost: armCostOf(run, report),
  };
}

interface PairView {
  from: string;
  to: string;
  view: ComparisonReportView | null;
  error: string | null;
  unavailable: string | null;
}

export function SkillComparePage() {
  const [params] = useSearchParams();
  const runsParam = params.get("runs") ?? "";
  const runIds = useMemo(
    () => runsParam.split(",").map((item) => item.trim()).filter(Boolean).slice(0, 3),
    [runsParam],
  );
  const [arms, setArms] = useState<ArmView[] | null>(null);
  const [pairs, setPairs] = useState<PairView[]>([]);
  const [error, setError] = useState("");
  const requestSeq = useRef(0);

  useEffect(() => {
    const seq = ++requestSeq.current;
    if (runIds.length === 0) {
      setArms([]);
      setPairs([]);
      setError("");
      return;
    }
    setArms(null);
    setError("");
    (async () => {
      try {
        const loaded = await Promise.all(runIds.map(async (id) => {
          const [run, report] = await Promise.all([
            getRun(id),
            getReport(id).catch(() => null),
          ]);
          return toArm(run, report);
        }));
        if (requestSeq.current !== seq) return; // 迟到的三臂数据不得覆盖新选择
        setArms(loaded);
      } catch (caught) {
        if (requestSeq.current !== seq) return;
        setError(describeApiError(caught).message);
        return;
      }
      const indices: Array<[number, number]> = [];
      for (let left = 0; left < runIds.length; left += 1) {
        for (let right = left + 1; right < runIds.length; right += 1) indices.push([left, right]);
      }
      const compared = await Promise.all(indices.map(async ([left, right]) => {
        const from = runIds[left];
        const to = runIds[right];
        try {
          const view = await compareRuns(from, to, "skill");
          return { from, to, view, error: null, unavailable: null };
        } catch (caught) {
          return {
            from,
            to,
            view: null,
            error: unavailableReason(caught) ? null : describeApiError(caught).message,
            unavailable: unavailableReason(caught),
          };
        }
      }));
      if (requestSeq.current !== seq) return;
      setPairs(compared);
    })();
  }, [runIds, runIds.length]);

  if (runIds.length === 0) {
    return (
      <div className="page">
        <section className="panel detail">
          <div className="panel-head"><h2>Skill · 三臂对照</h2></div>
          <p className="empty">
            暂无可对照运行。请在 URL 上给出三个 Run 引用（no-skill / skill-v1 / skill-v2）：
            <span className="mono"> /skill/compare?runs=run-a,run-b,run-c</span>
          </p>
        </section>
      </div>
    );
  }

  return (
    <div className="page">
      <section className="panel detail" aria-label="Skill 三臂对照">
        <div className="panel-head">
          <h2>Skill · 三臂对照（no-skill / skill-v1 / skill-v2）</h2>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        {!arms && !error && <p className="empty">加载中</p>}
        {arms && arms.length > 0 && (
          <>
            <p className="hint">
              三臂必须固定 DatasetVersion、样本集合、Agent/runtime、模型、scorer、工具权限与预算政策，
              并为每组使用独立初始状态与 session；只有 Skill 是允许变化的维度。
            </p>
            <table>
              <thead>
                <tr>
                  <th>条件</th>
                  {arms.map((arm) => (
                    <th key={arm.runId}>
                      <span className="mono">{arm.arm}</span>
                      <Link className="link" to={"/scenario/runs/" + encodeURIComponent(arm.runId)}>{arm.runId}</Link>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>模型</td>
                  {arms.map((arm) => <td key={arm.runId} className="mono">{arm.model}</td>)}
                </tr>
                {ARM_CONDITIONS.filter((condition) => condition.label !== "模型").map((condition) => (
                  <tr key={condition.label}>
                    <td>{condition.label}</td>
                    {arms.map((arm) => (
                      <td key={arm.runId} className="mono">{arm.conditions[condition.label]}</td>
                    ))}
                  </tr>
                ))}
                <tr>
                  <td>Skill 引用</td>
                  {arms.map((arm) => (
                    <td key={arm.runId} className="mono">
                      {arm.skillRefs.length === 0 ? "无 Skill" : arm.skillRefs.join(", ")}
                    </td>
                  ))}
                </tr>
                <tr>
                  <td>样本数</td>
                  {arms.map((arm) => <td key={arm.runId} className="mono">{arm.caseCount ?? UNKNOWN_TEXT}</td>)}
                </tr>
                <tr>
                  <td>通过 / 评分数</td>
                  {arms.map((arm) => (
                    <td key={arm.runId} className="mono">
                      {arm.passed == null || arm.scored == null ? UNKNOWN_TEXT : arm.passed + " / " + arm.scored}
                    </td>
                  ))}
                </tr>
                <tr>
                  <td>Skill token 开销</td>
                  {arms.map((arm) => (
                    <td key={arm.runId} className="mono" data-testid={"arm-overhead-" + arm.runId}>
                      {arm.skillTokenOverhead ?? UNKNOWN_TEXT}
                    </td>
                  ))}
                </tr>
                <tr>
                  <td>工具调用次数</td>
                  {arms.map((arm) => (
                    <td key={arm.runId} className="mono" data-testid={"arm-tool-calls-" + arm.runId}>
                      {arm.toolCalls ?? UNKNOWN_TEXT}
                    </td>
                  ))}
                </tr>
                <tr>
                  <td>报道成本</td>
                  {arms.map((arm) => (
                    <td key={arm.runId} className="mono">{arm.cost.reported_usd == null ? UNKNOWN_TEXT : "$" + arm.cost.reported_usd}</td>
                  ))}
                </tr>
                <tr>
                  <td>估算成本</td>
                  {arms.map((arm) => (
                    <td key={arm.runId} className="mono">{arm.cost.estimated_usd == null ? UNKNOWN_TEXT : "$" + arm.cost.estimated_usd}</td>
                  ))}
                </tr>
                <tr>
                  <td>成本完整性</td>
                  {arms.map((arm) => (
                    <td key={arm.runId} data-testid={"arm-cost-" + arm.runId}>
                      {arm.cost.unknown_cost
                        ? "未知" + (arm.cost.unknown_calls != null ? "（" + arm.cost.unknown_calls + " 个样本未报道成本）" : "")
                        : "已知"}
                    </td>
                  ))}
                </tr>
              </tbody>
            </table>
            <p className="hint">
              报道成本只覆盖已报道成本的样本；覆盖不完整时总额按未知处理，Skill 额外 token 也不重复计入模型费用。
            </p>

            <h3 className="embed-title">条件差异（配对比较）</h3>
            {pairs.length === 0 && <p className="empty">暂无配对结论</p>}
            {pairs.map((pair) => (
              <div key={pair.from + "->" + pair.to} className="drill-detail">
                <p className="mono">{pair.from + " → " + pair.to}</p>
                {pair.unavailable && (
                  <CapabilityNotice testId="skill-comparison-unavailable">
                    比较能力不可用：{pair.unavailable}。差异结论缺失时不得自行下结论。
                  </CapabilityNotice>
                )}
                {pair.error && <p className="error">{pair.error}</p>}
                {pair.view && (
                  <>
                    <p>
                      <span className="field-label">可比</span>
                      <StatusBadge status={pair.view.eligible ? "passed" : "failed"} />
                      <span className="field-label">质量指标可比</span>
                      <span className="mono">{String(pair.view.metric_eligibility?.quality ?? false)}</span>
                      <span className="field-label">成本指标可比</span>
                      <span className="mono">{String(pair.view.metric_eligibility?.cost ?? false)}</span>
                    </p>
                    {(pair.view.allowed_differences ?? []).length > 0 && (
                      <ul className="failure-list">
                        {(pair.view.allowed_differences ?? []).map((item) => (
                          <li key={item} className="mono">{item}</li>
                        ))}
                      </ul>
                    )}
                    {(pair.view.reasons ?? []).length > 0 && (
                      <ul className="failure-list">
                        {(pair.view.reasons ?? []).map((item) => (
                          <li key={item} className="mono fail">{item}</li>
                        ))}
                      </ul>
                    )}
                    {(pair.view.reasons ?? []).length === 0 && (
                      <p className="hint">没有阻断差异。</p>
                    )}
                  </>
                )}
              </div>
            ))}
          </>
        )}
      </section>
    </div>
  );
}
