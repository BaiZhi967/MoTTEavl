import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Board } from "../../board/Board";
import { EmptyBoard } from "../../board/EmptyBoard";
import { StatusFlap } from "../../board/StatusFlap";
import { ArrowClockwiseIcon, FlagCheckeredIcon } from "@phosphor-icons/react";
import {
  deprecateGatePolicy,
  describeApiError,
  evaluateVersionedGate,
  gateResultExportUrl,
  getGatePolicies,
  getGateResult,
  publishGatePolicy,
  type GatePolicyView,
  type GateResultFullView,
} from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { LoadFailure, UNKNOWN_TEXT } from "../../components/capability";

/**
 * 门禁页（M6）：政策清单（policy_id@version + lifecycle）→ 发布 / 弃用 →
 * 版本化求值（只读固定证据，零模型调用）→ 结果卡（六类决策 + 逐规则表 +
 * 导出 json/junit）。决策与规则状态徽章全部取自 STATUS_META（scope gate/rule）。
 */

/** RuleResult.status → STATUS_META 键（pass/fail 复用 passed/failed 条目）。 */
export function ruleBadgeStatus(status: string): string {
  if (status === "pass") return "passed";
  if (status === "fail") return "failed";
  return status; // insufficient / not_applicable / skipped_diagnostic 已登记；未知原样显示
}

function GateResultCard({ result }: { result: GateResultFullView }) {
  const rules = result.rule_results ?? [];
  return (
    <section className="panel detail" aria-label="Gate 求值结果">
      <div className="panel-head">
        <h2>Gate 求值结果</h2>
        <div className="panel-head-actions">
          <StatusBadge status={result.decision} />
          <span className="hint mono">退出码 {result.exit_code ?? UNKNOWN_TEXT}</span>
        </div>
      </div>
      <dl className="kv" data-testid="gate-result-kv">
        <dt>gate_result_id</dt>
        <dd className="mono">{result.gate_result_id}</dd>
        <dt>政策</dt>
        <dd className="mono">{result.policy_id + "@" + result.policy_version}</dd>
        <dt>求值时间</dt>
        <dd className="mono">{result.evaluated_at ?? UNKNOWN_TEXT}</dd>
        <dt>baseline</dt>
        <dd className="mono">
          {result.baseline
            ? [result.baseline.baseline_id, result.baseline.run_id, result.baseline.scoring_pass_id]
                .filter(Boolean).join(" · ") || UNKNOWN_TEXT
            : "（无：本次求值未引用 baseline）"}
        </dd>
        <dt>候选报告</dt>
        <dd className="mono">
          {(result.candidates ?? []).map((candidate) => candidate.run_id).filter(Boolean).join(", ") || UNKNOWN_TEXT}
        </dd>
        <dt>conclusion_hash</dt>
        <dd className="mono">{result.conclusion_hash ?? UNKNOWN_TEXT}</dd>
        <dt>evaluation_input_hash</dt>
        <dd className="mono">{result.evaluation_input_hash ?? UNKNOWN_TEXT}</dd>
        <dt>result_semantics_hash</dt>
        <dd className="mono">{result.result_semantics_hash ?? UNKNOWN_TEXT}</dd>
      </dl>

      <h3 className="embed-title">逐规则结果</h3>
      <Board
        testId="gate-rules-table"
        label="逐规则结果板面"
        head={
          <>
            <th className="w-[220px]">rule</th>
            <th className="w-[180px]">kind</th>
            <th className="w-[130px]">状态</th>
            <th className="w-[110px]">severity</th>
            <th>reason</th>
          </>
        }
      >
          {rules.map((rule) => (
            <tr key={rule.rule_id}>
              <td className="mono nowrap">{rule.rule_id}</td>
              <td className="mono nowrap">{rule.kind}</td>
              <td><StatusFlap status={ruleBadgeStatus(rule.status)} /></td>
              <td className="mono">{rule.severity ?? UNKNOWN_TEXT}</td>
              <td>{rule.reason}</td>
            </tr>
          ))}
          {rules.length === 0 && <tr><td colSpan={5} className="empty">暂无规则结果</td></tr>}
      </Board>
      <p className="hint">JSON 永远保留全部 rule results；warn 规则失败只记录，不改决策；退出码只是稳定摘要。</p>

      {(result.suggested_actions ?? []).length > 0 && (
        <>
          <h3 className="embed-title">建议动作</h3>
          <ul className="failure-list">
            {(result.suggested_actions ?? []).map((action) => (
              <li key={action} className="hint">{action}</li>
            ))}
          </ul>
        </>
      )}

      <div className="actions" data-testid="gate-export-links">
        <a className="link" href={gateResultExportUrl(result.gate_result_id, "json")} target="_blank" rel="noreferrer">
          导出 JSON
        </a>
        <a className="link" href={gateResultExportUrl(result.gate_result_id, "junit")} target="_blank" rel="noreferrer">
          导出 JUnit XML
        </a>
      </div>
    </section>
  );
}

const DEFAULT_POLICY = `{
  "policy_id": "release-gate",
  "version": "1",
  "lifecycle": "published",
  "diagnostic": false,
  "created_by": "web",
  "reason": "发布原因",
  "rules": [
    { "rule_id": "accuracy-floor", "kind": "metric_threshold", "metric_id": "accuracy", "operator": "gte", "threshold": 0.8 }
  ]
}`;

function PublishPolicyCard({ onPublished }: { onPublished: (policy: GatePolicyView) => void }) {
  const [text, setText] = useState(DEFAULT_POLICY);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const onPublish = async (event: FormEvent) => {
    event.preventDefault();
    let draft: unknown;
    try {
      draft = JSON.parse(text);
    } catch (parseError) {
      setError("政策 JSON 无法解析：" + (parseError instanceof Error ? parseError.message : String(parseError)));
      return;
    }
    if (draft === null || typeof draft !== "object" || Array.isArray(draft)) {
      setError("政策必须是 JSON 对象（policy_id / version / rules 必填）");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const stored = await publishGatePolicy(draft as Record<string, unknown>);
      setNotice(`已发布 ${stored.policy_id}@${stored.version}（不可变：同 id@version 异内容会被 409 拒绝）。`);
      onPublished(stored);
    } catch (caught) {
      const described = describeApiError(caught);
      setError(`发布失败${described.code ? "（" + described.code + "）" : ""}：${described.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel form-panel" aria-label="发布 Gate 政策">
      <form onSubmit={onPublish}>
        <h2>发布政策</h2>
        <label htmlFor="gate-policy-json">
          GatePolicyVersion（JSON）
          <textarea
            id="gate-policy-json"
            className="mono"
            rows={14}
            value={text}
            onChange={(change) => setText(change.target.value)}
          />
        </label>
        <div className="actions">
          <button type="submit" className="primary" disabled={busy} data-testid="gate-publish-submit">
            {busy ? "发布中…" : "发布政策版本"}
          </button>
        </div>
        {error && <p className="error" role="alert" data-testid="gate-publish-error">{error}</p>}
        {notice && <p className="hint pass" data-testid="gate-publish-notice">{notice}</p>}
      </form>
    </section>
  );
}

function PolicyDetail({
  policy,
  onRefresh,
  onEvaluated,
}: {
  policy: GatePolicyView;
  onRefresh: () => void;
  onEvaluated: (result: GateResultFullView) => void;
}) {
  const [runId, setRunId] = useState("");
  const [passId, setPassId] = useState("");
  const [baselineId, setBaselineId] = useState("");
  const [busy, setBusy] = useState<"evaluate" | "deprecate" | null>(null);
  const [confirmDeprecate, setConfirmDeprecate] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const evaluateSeq = useRef(0);

  const key = policy.policy_id + "@" + policy.version;
  const deprecated = policy.lifecycle === "deprecated";

  const onEvaluate = async (event: FormEvent) => {
    event.preventDefault();
    if (runId.trim() === "") return;
    const seq = ++evaluateSeq.current;
    setBusy("evaluate");
    setError("");
    setNotice("");
    try {
      const result = await evaluateVersionedGate({
        run_id: runId.trim(),
        policy_id: policy.policy_id,
        policy_version: policy.version,
        scoring_pass_id: passId.trim() === "" ? undefined : passId.trim(),
        baseline_id: baselineId.trim() === "" ? undefined : baselineId.trim(),
      });
      if (evaluateSeq.current !== seq) return; // 迟到结果不得覆盖新一次求值
      onEvaluated(result);
    } catch (caught) {
      if (evaluateSeq.current !== seq) return;
      const described = describeApiError(caught);
      setError(`求值失败${described.code ? "（" + described.code + "）" : ""}：${described.message}`);
    } finally {
      if (evaluateSeq.current === seq) setBusy(null);
    }
  };

  const onDeprecate = async () => {
    setBusy("deprecate");
    setError("");
    try {
      await deprecateGatePolicy(policy.policy_id, policy.version);
      setNotice("已弃用：历史 GateResult 引用不受影响，政策不参与新求值的默认选择。");
      setConfirmDeprecate(false);
      onRefresh();
    } catch (caught) {
      setError("弃用失败：" + describeApiError(caught).message);
    } finally {
      setBusy(null);
    }
  };

  const rules = policy.rules ?? [];

  return (
    <section className="panel wide-panel" aria-label="Gate 政策详情">
      <div className="panel-head">
        <h2>政策详情</h2>
        <div className="panel-head-actions">
          {policy.lifecycle && <StatusBadge status={policy.lifecycle} />}
          <button type="button" className="icon-btn" aria-label="刷新政策清单" onClick={onRefresh}>
            <ArrowClockwiseIcon size={14} weight="bold" aria-hidden />
          </button>
        </div>
      </div>
      <dl className="kv" data-testid="gate-policy-kv">
        <dt>政策</dt>
        <dd className="mono">{key}</dd>
        <dt>诊断政策</dt>
        <dd className="mono">{policy.diagnostic === true ? "是（整体永不输出 pass）" : "否（正式，fail-closed）"}</dd>
        <dt>创建者 / 理由</dt>
        <dd>{(policy.created_by ?? UNKNOWN_TEXT) + " · " + (policy.reason ?? UNKNOWN_TEXT)}</dd>
        <dt>创建时间</dt>
        <dd className="mono">{policy.created_at ?? UNKNOWN_TEXT}</dd>
      </dl>

      <h3 className="embed-title">规则（{rules.length}）</h3>
      <table>
        <thead><tr><th>rule_id</th><th>kind</th><th>metric</th><th>operator / threshold</th><th>severity</th><th>missing_policy</th></tr></thead>
        <tbody>
          {rules.map((rule) => (
            <tr key={String(rule.rule_id)}>
              <td className="mono nowrap">{String(rule.rule_id)}</td>
              <td className="mono nowrap">{String(rule.kind ?? UNKNOWN_TEXT)}</td>
              <td className="mono">{String(rule.metric_id ?? "—")}</td>
              <td className="mono">
                {rule.operator != null || rule.threshold != null
                  ? `${rule.operator ?? "?"} ${rule.threshold ?? rule.max_regression ?? "?"}`
                  : "—"}
              </td>
              <td className="mono">{String(rule.severity ?? "block")}</td>
              <td className="mono">{String(rule.missing_policy ?? "fail_closed")}</td>
            </tr>
          ))}
          {rules.length === 0 && <tr><td colSpan={6} className="empty">暂无规则</td></tr>}
        </tbody>
      </table>
      {notice && <p className="hint pass" data-testid="gate-policy-notice">{notice}</p>}
      {error && <p className="error" role="alert" data-testid="gate-policy-error">{error}</p>}

      <div className="actions">
        {confirmDeprecate ? (
          <>
            <button type="button" className="link danger" disabled={busy !== null} onClick={() => void onDeprecate()}>
              {busy === "deprecate" ? "弃用中…" : "确认弃用"}
            </button>
            <button type="button" className="link" disabled={busy !== null} onClick={() => setConfirmDeprecate(false)}>
              放弃
            </button>
          </>
        ) : (
          !deprecated && (
            <button type="button" className="link danger" disabled={busy !== null} onClick={() => setConfirmDeprecate(true)}>
              弃用该版本
            </button>
          )
        )}
      </div>

      <form onSubmit={onEvaluate}>
        <h3 className="embed-title">求值（只读固定证据，零模型调用）</h3>
        <label htmlFor="gate-evaluate-run">
          Run（必填，须为终态）
          <input
            id="gate-evaluate-run"
            className="mono"
            value={runId}
            placeholder="run id"
            onChange={(change) => setRunId(change.target.value)}
          />
        </label>
        <label htmlFor="gate-evaluate-pass">
          ScoringPass（可选，缺省 current）
          <input
            id="gate-evaluate-pass"
            className="mono"
            value={passId}
            placeholder="scoring pass id"
            onChange={(change) => setPassId(change.target.value)}
          />
        </label>
        <label htmlFor="gate-evaluate-baseline">
          Baseline（可选，固定快照引用）
          <input
            id="gate-evaluate-baseline"
            className="mono"
            value={baselineId}
            placeholder="baseline id"
            onChange={(change) => setBaselineId(change.target.value)}
          />
        </label>
        <div className="actions">
          <button
            type="submit"
            className="primary"
            disabled={busy !== null || runId.trim() === "" || deprecated}
            title={deprecated ? "已弃用政策不参与新求值的默认选择" : undefined}
            data-testid="gate-evaluate-submit"
          >
            {busy === "evaluate" ? "求值中…" : "求值 " + key}
          </button>
        </div>
      </form>
    </section>
  );
}

function ResultLookup({ onLoaded }: { onLoaded: (result: GateResultFullView) => void }) {
  const [resultId, setResultId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);

  const onLoad = async (event: FormEvent) => {
    event.preventDefault();
    const target = resultId.trim();
    if (target === "") return;
    const current = ++seq.current;
    setBusy(true);
    setError("");
    try {
      const result = await getGateResult(target);
      if (seq.current !== current) return;
      onLoaded(result);
    } catch (caught) {
      if (seq.current !== current) return;
      setError("读取结果失败：" + describeApiError(caught).message);
    } finally {
      if (seq.current === current) setBusy(false);
    }
  };

  return (
    <section className="panel form-panel" aria-label="按 ID 读取 Gate 结果">
      <form onSubmit={onLoad}>
        <h2>读取既有结果</h2>
        <label htmlFor="gate-result-id">
          gate_result_id
          <input
            id="gate-result-id"
            className="mono"
            value={resultId}
            placeholder="sha256:…"
            onChange={(change) => setResultId(change.target.value)}
          />
        </label>
        <div className="actions">
          <button type="submit" className="primary" disabled={busy || resultId.trim() === ""}>
            {busy ? "读取中…" : "读取结果"}
          </button>
        </div>
        {error && <p className="error" role="alert" data-testid="gate-result-lookup-error">{error}</p>}
      </form>
    </section>
  );
}

export function GatePage() {
  const [policies, setPolicies] = useState<GatePolicyView[]>([]);
  const [listError, setListError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [result, setResult] = useState<GateResultFullView | null>(null);

  const refresh = useCallback(async () => {
    setLoaded(false);
    try {
      const payload = await getGatePolicies();
      setPolicies(payload.items);
      setListError(null);
    } catch (caught) {
      setPolicies([]);
      setListError(caught);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const policy = policies.find((item) => item.policy_id + "@" + item.version === selected)
    ?? (policies.length > 0 ? policies[0] : null);

  return (
    <div className="page">
      <aside className="panel list-panel" aria-label="Gate 政策清单">
        <div className="panel-head">
          <h2>Gate 政策</h2>
          <div className="panel-head-actions">
            <button type="button" className="icon-btn" aria-label="刷新政策清单" onClick={() => void refresh()}>
              <ArrowClockwiseIcon size={14} weight="bold" aria-hidden />
            </button>
          </div>
        </div>
        {listError !== null && <LoadFailure error={listError} what="Gate 政策清单" testId="gate-list-unavailable" />}
        {!loaded && policies.length === 0 ? (
          <p className="empty">加载中</p>
        ) : policies.length === 0 ? (
          <p className="empty" data-testid="gate-list-empty">暂无 Gate 政策</p>
        ) : (
          <ul className="provider-list">
            {policies.map((item) => {
              const key = item.policy_id + "@" + item.version;
              return (
                <li key={key}>
                  <button
                    type="button"
                    className="provider-item"
                    data-state={policy && policy.policy_id + "@" + policy.version === key ? "active" : undefined}
                    onClick={() => setSelected(key)}
                  >
                    <FlagCheckeredIcon size={16} weight="bold" aria-hidden />
                    <span className="provider-item-name mono">{key}</span>
                    {item.lifecycle && <StatusBadge status={item.lifecycle} />}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </aside>

      {policy
        ? <PolicyDetail policy={policy} onRefresh={() => void refresh()} onEvaluated={setResult} />
        : <section className="panel wide-panel" aria-label="Gate 政策详情">
            <h2>政策详情</h2>
            <p className="empty">选择左侧（或先发布）一个政策版本。</p>
          </section>}

      <PublishPolicyCard
        onPublished={(stored) => {
          setPolicies((items) => {
            const key = stored.policy_id + "@" + stored.version;
            const index = items.findIndex((item) => item.policy_id + "@" + item.version === key);
            if (index === -1) return [...items, stored];
            const next = [...items];
            next[index] = stored;
            return next;
          });
          setSelected(stored.policy_id + "@" + stored.version);
        }}
      />

      <ResultLookup onLoaded={setResult} />

      {result && <GateResultCard result={result} />}
    </div>
  );
}
