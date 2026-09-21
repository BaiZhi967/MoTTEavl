import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import * as Tabs from "@radix-ui/react-tabs";
import { ArrowClockwiseIcon, FlowArrowIcon, ShieldCheckIcon } from "@phosphor-icons/react";
import {
  getRun,
  getScenarioRunSteps,
  getWorkflow,
  getWorkflows,
  publishWorkflow,
  validateWorkflow,
  validationIssuesFromError,
  type RunRecord,
  type ScenarioFixtureState,
  type ScenarioRunStepsView,
  type ScenarioStepState,
  type ValidationIssue,
  type WorkflowValidationReport,
  type WorkflowVersionRecord,
} from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import {
  CapabilityNotice,
  LoadFailure,
  UNKNOWN_TEXT,
  UnknownValue,
  failureText,
  unavailableReason,
} from "../../components/capability";
import { readPath } from "../../components/jsonValues";
import { formatClock } from "../../components/runFormat";

/**
 * Workflow 编辑器：文本（JSON）与 schema 字段两种编辑方式作用于同一份草案。
 * 本地只判「JSON 语法 + 必填字段是否存在」，DSL 语义（重复 step_id、无界循环、
 * 未知引用、预算上限）一律由只读校验接口给出逐字段错误，不在这里重复实现规则。
 */

const DRAFT_TEMPLATE = {
  workflow_id: "",
  version: "1",
  description: "",
  limits: { max_total_steps: 20, max_turns: 4, wall_time_sec: 30 },
  failure_policy: "stop_case",
  fixture_refs: [],
  steps: [],
};

export function emptyWorkflowDraftText(): string {
  return JSON.stringify(DRAFT_TEMPLATE, null, 2);
}

export interface ParsedDraft {
  value: Record<string, unknown> | null;
  issues: ValidationIssue[];
}

/** JSON 语法解析：语法错误定位到 workflow 根，附带解析器给出的位置说明。 */
export function parseWorkflowDraft(text: string): ParsedDraft {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (error) {
    return {
      value: null,
      issues: [{
        locator: "workflow",
        code: "JSON_INVALID",
        message: "JSON 语法错误：" + (error instanceof Error ? error.message : String(error)),
      }],
    };
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return {
      value: null,
      issues: [{ locator: "workflow", code: "WORKFLOW_NOT_OBJECT", message: "顶层必须是一个 JSON 对象" }],
    };
  }
  return { value: value as Record<string, unknown>, issues: [] };
}

/** 本地必填检查：只覆盖编辑器能确知的存在性，不替代服务端 DSL 校验。 */
export function localSchemaIssues(draft: Record<string, unknown> | null): ValidationIssue[] {
  if (!draft) return [];
  const issues: ValidationIssue[] = [];
  if (typeof draft.workflow_id !== "string" || draft.workflow_id.trim() === "") {
    issues.push({ locator: "workflow_id", code: "REQUIRED", message: "workflow_id 必填" });
  }
  if (typeof draft.version !== "string" || draft.version.trim() === "") {
    issues.push({ locator: "version", code: "REQUIRED", message: "version 必填" });
  }
  if (draft.steps !== undefined && (!Array.isArray(draft.steps) || draft.steps.length === 0)) {
    issues.push({ locator: "steps", code: "REQUIRED", message: "至少需要一个步骤（steps）" });
  }
  return issues;
}

/** 字段路径匹配：locator === prefix 或位于其子树（steps / steps[0] / steps[0].kind）。 */
export function issuesForLocator(issues: ValidationIssue[], prefix: string): ValidationIssue[] {
  return issues.filter((issue) => {
    const locator = issue.locator ?? "";
    return locator === prefix || locator.startsWith(prefix + ".") || locator.startsWith(prefix + "[");
  });
}

function writePath(source: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> {
  const next = JSON.parse(JSON.stringify(source)) as Record<string, unknown>;
  const segments = path.split(".");
  let cursor: Record<string, unknown> = next;
  for (const segment of segments.slice(0, -1)) {
    const child = cursor[segment];
    if (child === null || typeof child !== "object" || Array.isArray(child)) cursor[segment] = {};
    cursor = cursor[segment] as Record<string, unknown>;
  }
  cursor[segments[segments.length - 1]] = value;
  return next;
}

function FieldErrors({ issues, locator }: { issues: ValidationIssue[]; locator: string }) {
  const matched = issuesForLocator(issues, locator);
  if (matched.length === 0) return null;
  return (
    <p className="hint fail" data-testid={"field-error-" + locator}>
      {matched.map((issue) => issue.code + "：" + issue.message).join("；")}
    </p>
  );
}

function StepStatusBadge({ step }: { step: ScenarioStepState }) {
  const status = stepStatusOf(step);
  return <StatusBadge status={status} />;
}

/** 步骤状态：服务端没给状态时是「未知」，绝不推断成成功或待执行。 */
export function stepStatusOf(step: ScenarioStepState): string {
  if (typeof step.status === "string" && step.status.trim() !== "") return step.status;
  return "unknown";
}

/** fixture 清理结论：cleanup_failed 是明确的失败，缺失则未知（不显示成已清理）。 */
export function cleanupStatusOf(fixture: ScenarioFixtureState): string {
  const status = fixture.cleanup?.status;
  if (typeof status === "string" && status.trim() !== "") {
    return status === "failed" || status === "error" ? "cleanup_failed" : status;
  }
  if (fixture.cleanup?.error) return "cleanup_failed";
  return "unknown";
}

function StepRow({ step, index }: { step: ScenarioStepState; index: number }) {
  const [open, setOpen] = useState(false);
  const assertions = step.assertions ?? [];
  const failedAssertions = assertions.filter((assertion) => assertion.passed === false);
  const unknownAssertions = assertions.filter((assertion) => assertion.passed !== true && assertion.passed !== false);
  return (
    <>
      <tr>
        <td className="mono nowrap">{step.index ?? index + 1}</td>
        <td className="mono nowrap">{step.step_id}</td>
        <td className="mono">{step.kind ?? UNKNOWN_TEXT}</td>
        <td><StepStatusBadge step={step} /></td>
        <td>
          {step.tool_mode
            ? <span className="status-badge status-tone-neutral mono">{step.tool_mode}</span>
            : <UnknownValue reason="该步骤没有受控工具模式（非工具步骤或服务端未登记）" />}
        </td>
        <td className="mono nowrap">
          {assertions.length === 0
            ? "—"
            : (assertions.length - failedAssertions.length - unknownAssertions.length) + "/" + assertions.length}
        </td>
        <td className="mono nowrap">{step.duration_ms == null ? UNKNOWN_TEXT : step.duration_ms + "ms"}</td>
        <td className="row-actions">
          <button type="button" className="link" onClick={() => setOpen(!open)} aria-expanded={open}>
            {open ? "收起" : "详情"}
          </button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={8}>
            <div className="drill-detail">
            <p>
              <span className="field-label">开始</span>
              <span className="mono">{formatClock(step.started_at) ?? UNKNOWN_TEXT}</span>
              <span className="field-label">结束</span>
              <span className="mono">{formatClock(step.finished_at) ?? UNKNOWN_TEXT}</span>
              <span className="field-label">尝试</span>
              <span className="mono">{step.attempts ?? UNKNOWN_TEXT}</span>
            </p>
            {step.checkpoint && (
              <p>
                <span className="field-label">checkpoint</span>
                <span className="mono">{step.checkpoint.label ?? step.step_id}</span>
                <span className="field-label">冻结</span>
                <span>{step.checkpoint.frozen === true ? "已冻结" : step.checkpoint.frozen === false ? "未冻结" : UNKNOWN_TEXT}</span>
                <span className="field-label">state_hash</span>
                <span className="mono">{step.checkpoint.state_hash ?? UNKNOWN_TEXT}</span>
              </p>
            )}
            {step.error && (
              <p className="error">
                {(step.error.code ?? "STEP_ERROR") + "：" + (step.error.message ?? "服务端未给出消息")}
              </p>
            )}
            {(step.unknown === true || unknownAssertions.length > 0) && (
              <CapabilityNotice>
                该步骤存在未知结果（服务端未给出结论）：不得当作成功。未知断言 {unknownAssertions.length} 条。
              </CapabilityNotice>
            )}
            {assertions.length > 0 && (
              <table>
                <thead>
                  <tr><th>断言</th><th>运算符</th><th>结果</th><th>说明</th></tr>
                </thead>
                <tbody>
                  {assertions.map((assertion, position) => (
                    <tr key={(assertion.locator ?? assertion.path ?? "assertion") + "-" + position}>
                      <td className="mono nowrap">{assertion.locator ?? assertion.path ?? UNKNOWN_TEXT}</td>
                      <td className="mono">{assertion.op ?? UNKNOWN_TEXT}</td>
                      <td>
                        {assertion.passed === true
                          ? <span className="pass">通过</span>
                          : assertion.passed === false
                            ? <span className="fail">未通过</span>
                            : <UnknownValue reason="服务端没有给出该断言的结论" />}
                      </td>
                      <td>{assertion.reason ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {step.detail && <p className="hint">{step.detail}</p>}
            {step.result !== undefined && step.result !== null && (
              <pre className="terminal-log">{JSON.stringify(step.result, null, 2)}</pre>
            )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function FixtureTable({ fixtures }: { fixtures: ScenarioFixtureState[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>fixture</th><th>版本</th><th>类型</th><th>owner</th><th>隔离</th><th>快照</th><th>清理</th><th>错误</th>
        </tr>
      </thead>
      <tbody>
        {fixtures.map((fixture) => {
          const cleanup = cleanupStatusOf(fixture);
          const isolationError = fixture.isolated === false || fixture.prepare_error != null;
          return (
            <tr key={fixture.fixture_id + "@" + (fixture.version ?? "?")}>
              <td className="mono nowrap">{fixture.fixture_id}</td>
              <td className="mono nowrap">{fixture.version ?? UNKNOWN_TEXT}</td>
              <td className="mono">{fixture.kind ?? UNKNOWN_TEXT}</td>
              <td className="mono nowrap">{fixture.owner ?? UNKNOWN_TEXT}</td>
              <td>
                {fixture.isolated === true
                  ? <span className="pass">已隔离</span>
                  : fixture.isolated === false
                    ? <span className="fail">未隔离</span>
                    : <UnknownValue reason="服务端没有给出隔离结论" />}
              </td>
              <td>
                {fixture.snapshot == null
                  ? <UnknownValue reason="没有快照引用" />
                  : fixture.snapshot.complete === true
                    ? <span className="pass mono">{fixture.snapshot.ref ?? "完整"}</span>
                    : <UnknownValue reason="快照不完整或引用缺失" />}
              </td>
              <td>
                <StatusBadge status={cleanup} />
                {fixture.cleanup?.residual && fixture.cleanup.residual.length > 0 && (
                  <span className="hint mono"> 残留 {fixture.cleanup.residual.length} 项</span>
                )}
              </td>
              <td>
                {isolationError && (
                  <span className="fail">
                    {fixture.prepare_error
                      ? (fixture.prepare_error.code ?? "FIXTURE_PREPARE_FAILED") + "：" + (fixture.prepare_error.message ?? "")
                      : "fixture 未隔离"}
                  </span>
                )}
                {fixture.cleanup?.error && <span className="fail">{fixture.cleanup.error}</span>}
                {!isolationError && !fixture.cleanup?.error && "—"}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function ScenarioWorkflowsPage() {
  const [items, setItems] = useState<WorkflowVersionRecord[]>([]);
  const [listError, setListError] = useState<unknown>(null);
  const [listLoaded, setListLoaded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [text, setText] = useState(emptyWorkflowDraftText);
  const [tab, setTab] = useState("text");
  const [report, setReport] = useState<WorkflowValidationReport | null>(null);
  const [validatedText, setValidatedText] = useState<string | null>(null);
  const [actionError, setActionError] = useState("");
  const [publishNotice, setPublishNotice] = useState("");
  const [busy, setBusy] = useState<"validate" | "publish" | null>(null);
  /** 迟到响应不得覆盖新选择：每次载入版本 +1，回调只在序号仍最新时写入。 */
  const loadSeq = useRef(0);

  const refresh = useCallback(async () => {
    setListLoaded(false);
    try {
      const payload = await getWorkflows();
      setItems(payload.items);
      setListError(null);
    } catch (error) {
      setItems([]);
      setListError(error);
    } finally {
      setListLoaded(true);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const parsed = useMemo(() => parseWorkflowDraft(text), [text]);
  const freshReport = validatedText === text ? report : null;
  const issues: ValidationIssue[] = [
    ...parsed.issues,
    ...localSchemaIssues(parsed.value),
    ...(freshReport?.errors ?? []),
  ];
  const canPublish = parsed.value !== null
    && parsed.issues.length === 0
    && localSchemaIssues(parsed.value).length === 0
    && freshReport?.ok === true
    && freshReport.publishable !== false
    && busy === null;

  const selectVersion = async (record: WorkflowVersionRecord) => {
    const seq = ++loadSeq.current;
    const key = record.workflow_id + "@" + record.version;
    setSelected(key);
    setReport(null);
    setValidatedText(null);
    setActionError("");
    setPublishNotice("");
    try {
      const full = await getWorkflow(record.workflow_id, record.version);
      if (loadSeq.current !== seq) return; // 已切换到其它版本：迟到响应不得覆盖
      setText(JSON.stringify(full, null, 2));
    } catch (error) {
      if (loadSeq.current !== seq) return; // 迟到的失败同样不得覆盖
      if (unavailableReason(error)) {
        setText(JSON.stringify(record, null, 2));
        setPublishNotice("版本详情端点不可用，已用清单记录载入；发布前请重新校验。");
        return;
      }
      setActionError(failureText(error, "载入版本"));
    }
  };

  const onValidate = async (event?: FormEvent) => {
    event?.preventDefault();
    if (parsed.value === null) return;
    const submitted = text;
    setBusy("validate");
    setActionError("");
    setPublishNotice("");
    try {
      const result = await validateWorkflow(parsed.value);
      setReport(result);
      setValidatedText(submitted);
    } catch (error) {
      const issues = validationIssuesFromError(error, "workflow");
      if (issues) {
        // 服务端 4xx 是**校验结论**（逐字段问题），不是读取故障：发布保持禁用
        setReport({ ok: false, errors: issues, warnings: [] });
        setValidatedText(submitted);
        setActionError("");
      } else {
        setReport(null);
        setValidatedText(null);
        // 校验失败（含端点未注册）都保留草案文本，不清空编辑器
        setActionError(failureText(error, "校验"));
      }
    } finally {
      setBusy(null);
    }
  };

  const onPublish = async () => {
    if (parsed.value === null || !canPublish) return;
    setBusy("publish");
    setActionError("");
    setPublishNotice("");
    try {
      const record = await publishWorkflow(parsed.value);
      setPublishNotice("已发布 " + record.workflow_id + "@" + record.version + "（content_hash " + (record.content_hash ?? UNKNOWN_TEXT) + "）");
      await refresh();
    } catch (error) {
      // 失败保留表单内容：只动错误显示，不动草案文本
      const issues = validationIssuesFromError(error, "workflow");
      if (issues) setReport({ ok: false, errors: issues, warnings: [] });
      else setActionError(failureText(error, "发布"));
    } finally {
      setBusy(null);
    }
  };

  const updateField = (path: string, value: unknown) => {
    if (parsed.value === null) return;
    setText(JSON.stringify(writePath(parsed.value, path, value), null, 2));
  };

  const listUnavailable = listError !== null && unavailableReason(listError) !== null;

  return (
    <div className="page">
      <aside className="panel list-panel" aria-label="Workflow 版本清单">
        <div className="panel-head">
          <h2>Workflow</h2>
          <div className="panel-head-actions">
            <button type="button" className="icon-btn" aria-label="刷新清单" onClick={() => void refresh()}>
              <ArrowClockwiseIcon size={14} weight="bold" aria-hidden />
            </button>
          </div>
        </div>
        {listError !== null && <LoadFailure error={listError} what="Workflow 清单" testId="workflow-list-unavailable" />}
        {!listLoaded && items.length === 0 ? (
          <p className="empty">加载中</p>
        ) : items.length === 0 ? (
          <p className="empty">暂无已发布 Workflow</p>
        ) : (
          <ul className="provider-list">
            {items.map((item) => {
              const key = item.workflow_id + "@" + item.version;
              return (
                <li key={key}>
                  <button
                    type="button"
                    className="provider-item"
                    data-state={selected === key ? "active" : undefined}
                    onClick={() => void selectVersion(item)}
                  >
                    <FlowArrowIcon size={16} weight="bold" aria-hidden />
                    <span className="provider-item-name mono">{key}</span>
                    <span className="status-badge status-tone-neutral mono">{item.lifecycle ?? UNKNOWN_TEXT}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </aside>

      <section className="panel wide-panel" aria-label="Workflow 编辑器">
        <div className="panel-head">
          <h2>Workflow 编辑器</h2>
          <span className="hint">只读校验不发布、不建 Run、不产生调用与费用</span>
        </div>
        <Tabs.Root value={tab} onValueChange={setTab}>
          <Tabs.List className="tabs-list" aria-label="编辑方式">
            <Tabs.Trigger className="tabs-trigger" value="text">文本（JSON）</Tabs.Trigger>
            <Tabs.Trigger className="tabs-trigger" value="schema">Schema 字段</Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="text">
            <form onSubmit={onValidate}>
              <label htmlFor="workflow-draft">
                WorkflowVersion DSL（JSON）
                <textarea
                  id="workflow-draft"
                  className="mono"
                  rows={18}
                  value={text}
                  onChange={(change) => setText(change.target.value)}
                  spellCheck={false}
                />
              </label>
              <FieldErrors issues={issues} locator="workflow" />
              <div className="actions">
                <button type="button" disabled={parsed.value === null || busy !== null} onClick={() => void onValidate()}>
                  {busy === "validate" ? "校验中…" : "校验（只读）"}
                </button>
                <button
                  type="button"
                  className="primary"
                  disabled={!canPublish}
                  title={canPublish ? undefined : "需先通过只读校验且文本未被修改"}
                  onClick={() => void onPublish()}
                >
                  {busy === "publish" ? "发布中…" : "发布版本"}
                </button>
              </div>
            </form>
          </Tabs.Content>
          <Tabs.Content value="schema">
            {parsed.value === null ? (
              <CapabilityNotice>
                草案不是合法 JSON 对象，Schema 字段不可编辑；请先修正文本标签页中的语法错误。
              </CapabilityNotice>
            ) : (
              <form onSubmit={onValidate}>
                <label htmlFor="field-workflow-id">
                  workflow_id
                  <input
                    id="field-workflow-id"
                    value={String(readPath(parsed.value, "workflow_id") ?? "")}
                    onChange={(change) => updateField("workflow_id", change.target.value)}
                  />
                </label>
                <FieldErrors issues={issues} locator="workflow_id" />
                <label htmlFor="field-version">
                  version
                  <input
                    id="field-version"
                    value={String(readPath(parsed.value, "version") ?? "")}
                    onChange={(change) => updateField("version", change.target.value)}
                  />
                </label>
                <FieldErrors issues={issues} locator="version" />
                <label htmlFor="field-max-steps">
                  limits.max_total_steps
                  <input
                    id="field-max-steps"
                    type="number"
                    value={String(readPath(parsed.value, "limits.max_total_steps") ?? "")}
                    onChange={(change) => updateField("limits.max_total_steps", change.target.value === "" ? null : Number(change.target.value))}
                  />
                </label>
                <FieldErrors issues={issues} locator="limits.max_total_steps" />
                <label htmlFor="field-max-turns">
                  limits.max_turns
                  <input
                    id="field-max-turns"
                    type="number"
                    value={String(readPath(parsed.value, "limits.max_turns") ?? "")}
                    onChange={(change) => updateField("limits.max_turns", change.target.value === "" ? null : Number(change.target.value))}
                  />
                </label>
                <FieldErrors issues={issues} locator="limits.max_turns" />
                <label htmlFor="field-wall-time">
                  limits.wall_time_sec
                  <input
                    id="field-wall-time"
                    type="number"
                    value={String(readPath(parsed.value, "limits.wall_time_sec") ?? "")}
                    onChange={(change) => updateField("limits.wall_time_sec", change.target.value === "" ? null : Number(change.target.value))}
                  />
                </label>
                <FieldErrors issues={issues} locator="limits.wall_time_sec" />
                <label htmlFor="field-failure-policy">
                  failure_policy
                  <select
                    id="field-failure-policy"
                    className="control"
                    value={String(readPath(parsed.value, "failure_policy") ?? "stop_case")}
                    onChange={(change) => updateField("failure_policy", change.target.value)}
                  >
                    <option value="stop_case">stop_case</option>
                    <option value="continue_for_evidence">continue_for_evidence</option>
                  </select>
                </label>
                <FieldErrors issues={issues} locator="failure_policy" />
                <FieldErrors issues={issues} locator="steps" />
                <div className="actions">
                  <button type="button" disabled={busy !== null} onClick={() => void onValidate()}>校验（只读）</button>
                  <button type="button" className="primary" disabled={!canPublish} onClick={() => void onPublish()}>
                    发布版本
                  </button>
                </div>
              </form>
            )}
          </Tabs.Content>
        </Tabs.Root>

        {validatedText !== null && validatedText !== text && (
          <CapabilityNotice testId="validation-stale">
            文本已修改，上一次校验结论已失效；发布前必须重新校验。
          </CapabilityNotice>
        )}
        {freshReport && (
          <p className="hint" data-testid="validation-result">
            <span className="field-label">校验结论</span>
            <StatusBadge status={freshReport.ok ? "passed" : "failed"} />
            <span className="mono"> errors={freshReport.errors.length} warnings={(freshReport.warnings ?? []).length}</span>
            {freshReport.step_count != null && <span className="mono"> steps={freshReport.step_count}</span>}
            {freshReport.condition_count != null && <span className="mono"> conditions={freshReport.condition_count}</span>}
            {freshReport.content_hash && <span className="mono"> content_hash={freshReport.content_hash}</span>}
          </p>
        )}
        {freshReport && freshReport.errors.length > 0 && (
          <ul className="failure-list">
            {freshReport.errors.map((issue) => (
              <li key={issue.locator + issue.code + issue.message}>
                <span className="mono">{issue.locator}</span>
                <span className="fail"> {issue.code}</span>
                <span className="hint"> {issue.message}</span>
              </li>
            ))}
          </ul>
        )}
        {parsed.issues.length > 0 && (
          <p className="hint">
            校验与发布在 JSON 语法或必填字段未满足时不可用
            {listUnavailable ? "；清单端点不可用，仍可在本地编辑草案" : ""}。
          </p>
        )}
        {actionError && <p className="error" role="alert" data-testid="workflow-action-error">{actionError}</p>}
        {publishNotice && <CapabilityNotice tone="info" testId="workflow-publish-notice">{publishNotice}</CapabilityNotice>}
        <p className="hint">
          <ShieldCheckIcon size={14} weight="bold" aria-hidden /> 发布前必须通过只读校验；校验只解释草案，不改动已发布版本。
        </p>
      </section>
    </div>
  );
}

export function ScenarioRunStepsPage() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [runError, setRunError] = useState<unknown>(null);
  const [steps, setSteps] = useState<ScenarioRunStepsView | null>(null);
  const [stepsError, setStepsError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const requestSeq = useRef(0);

  const reload = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoaded(false);
    try {
      const record = await getRun(runId);
      if (requestSeq.current !== seq) return; // 迟到的 Run 不得覆盖新 Run
      setRun(record);
      setRunError(null);
    } catch (error) {
      if (requestSeq.current !== seq) return;
      setRun(null);
      setRunError(error);
    }
    try {
      const view = await getScenarioRunSteps(runId);
      if (requestSeq.current !== seq) return; // 迟到的步骤证据不得覆盖新 Run
      setSteps(view);
      setStepsError(null);
    } catch (error) {
      if (requestSeq.current !== seq) return;
      setSteps(null);
      setStepsError(error);
    } finally {
      if (requestSeq.current === seq) setLoaded(true);
    }
  }, [runId]);

  useEffect(() => {
    setRun(null);
    setSteps(null);
    setStepsError(null);
    void reload();
  }, [reload]);

  const stepsUnavailable = stepsError !== null && unavailableReason(stepsError) !== null;
  const unknownSteps = (steps?.steps ?? []).filter((step) => stepStatusOf(step) === "unknown").length;
  const cleanupFailures = (steps?.fixtures ?? []).filter((fixture) => cleanupStatusOf(fixture) === "cleanup_failed").length;

  return (
    <div className="page">
      <section className="panel detail" aria-label="场景运行步骤">
        <div className="panel-head">
          <h2>场景运行 · 步骤与 checkpoint</h2>
          <div className="panel-head-actions">
            <span className="hint mono">{runId}</span>
            <button type="button" className="icon-btn" aria-label="刷新步骤" onClick={() => void reload()}>
              <ArrowClockwiseIcon size={14} weight="bold" aria-hidden />
            </button>
          </div>
        </div>

        {runError !== null && <LoadFailure error={runError} what="运行" testId="scenario-run-unavailable" />}
        {run && (
          <dl className="kv">
            <dt>状态</dt>
            <dd><StatusBadge status={run.status} /></dd>
            <dt>场景</dt>
            <dd className="mono">{run.scenario_version}</dd>
            <dt>Workflow</dt>
            <dd className="mono">
              {steps?.workflow
                ? (steps.workflow.workflow_id ?? UNKNOWN_TEXT) + "@" + (steps.workflow.version ?? UNKNOWN_TEXT)
                : UNKNOWN_TEXT}
            </dd>
          </dl>
        )}
        {!loaded && !run && runError === null && <p className="empty">加载中</p>}

        {stepsError !== null && (
          <LoadFailure error={stepsError} what="逐步证据" testId="scenario-steps-unavailable" />
        )}
        {stepsUnavailable && (
          <CapabilityNotice tone="info">
            步骤明细不可用时仍显示运行自身状态；不会用运行状态替代每步结论。
          </CapabilityNotice>
        )}

        {steps && (
          <>
            <p className="hint">
              步骤 {steps.steps.length} 个，未知结果 {unknownSteps} 个，清理失败 {cleanupFailures} 个。
              未知结果不计入成功。
            </p>
            <table>
              <thead>
                <tr>
                  <th>#</th><th>step_id</th><th>类型</th><th>状态</th><th>工具模式</th><th>断言</th><th>耗时</th><th />
                </tr>
              </thead>
              <tbody>
                {steps.steps.map((step, index) => (
                  <StepRow key={step.step_id + "-" + index} step={step} index={index} />
                ))}
                {steps.steps.length === 0 && (
                  <tr><td colSpan={8} className="empty">暂无步骤证据</td></tr>
                )}
              </tbody>
            </table>
          </>
        )}
      </section>

      {steps && (steps.fixtures ?? []).length > 0 && (
        <section className="panel detail" aria-label="Fixture 隔离与清理">
          <h2>Fixture 隔离与清理</h2>
          <FixtureTable fixtures={steps.fixtures ?? []} />
        </section>
      )}

      {steps && (steps.fixtures ?? []).length === 0 && (
        <section className="panel detail">
          <h2>Fixture 隔离与清理</h2>
          <p className="empty">本运行没有登记 fixture；隔离与清理结论不适用。</p>
        </section>
      )}

    </div>
  );
}
