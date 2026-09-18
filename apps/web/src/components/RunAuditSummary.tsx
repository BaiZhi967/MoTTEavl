import type { RunRecord } from "../api/client";

function versioned(id: unknown, version: unknown): string {
  if (typeof id !== "string" || !id) return "未固定";
  return typeof version === "string" && version ? `${id}@${version}` : id;
}

function identityResults(run: RunRecord): Record<string, any>[] {
  return (run.cases ?? [])
    .map((row) => row.result)
    .filter((result): result is Record<string, any> => Boolean(
      result && typeof result === "object" && (
        typeof result.requested_model === "string"
        || typeof result.reported_model === "string"
        || typeof result.resolved_model_identity === "string"
      )
    ));
}

function fieldSummary(identities: Record<string, any>[], key: string): string {
  const values = [...new Set(identities
    .map((identity) => identity[key])
    .filter((value): value is string => typeof value === "string" && Boolean(value)))];
  if (values.length === 0) return "未报告";
  if (values.length === 1) return values[0];
  return `${values.length} 种取值`;
}

function shortHash(hash: unknown): string {
  if (typeof hash !== "string" || !hash) return "—";
  return hash.length > 16 ? `${hash.slice(0, 12)}…` : hash;
}

/* 身份判定词汇（对齐 contracts 的 IdentityVerdict 五值；组件本地词汇表，同 OUTCOME_LABELS 模式）。 */
const IDENTITY_VERDICT_LABELS: Record<string, string> = {
  exact_match: "完全一致",
  alias_match: "别名一致",
  unreported: "未报告",
  mismatch: "不一致",
  not_evaluated: "未判定",
};

function verdictSummary(identities: Record<string, any>[]): string {
  const values = [...new Set(identities
    .map((identity) => identity.identity_policy_result)
    .filter((value): value is string => typeof value === "string" && Boolean(value)))];
  if (values.length === 0) return IDENTITY_VERDICT_LABELS.not_evaluated;
  if (values.length === 1) return IDENTITY_VERDICT_LABELS[values[0]] ?? values[0];
  return "多种判定";
}

export function RunAuditSummary({ run }: { run: RunRecord }) {
  const manifest = run.manifest ?? {};
  const execution = manifest.execution ?? {};
  const evaluation = manifest.evaluation ?? {};
  const provider = manifest.provider ?? {};
  const snapshots = manifest.resource_snapshots ?? {};
  const modelSnapshot = snapshots.model_profile ?? {};
  const providerSnapshot = snapshots.provider_connection ?? {};
  const scoringPass = run.scoring_pass;
  const identities = identityResults(run);

  return (
    <section className="embedded" aria-label="运行审计快照">
      <h3 className="embed-title">运行审计快照</h3>
      <dl className="kv">
        <dt>Run schema / revision</dt>
        <dd className="mono">v{run.schema_version ?? 1} / r{run.revision ?? 0}</dd>
        <dt>ExecutionBackend</dt>
        <dd className="mono">{versioned(execution.backend_id, execution.backend_version)}</dd>
        <dt>Provider adapter</dt>
        <dd className="mono">{versioned(provider.adapter_id ?? provider.kind, provider.adapter_version ?? provider.implementation_version)}</dd>
        <dt>Evaluation adapter</dt>
        <dd className="mono">{versioned(evaluation.adapter_id, evaluation.adapter_version)}</dd>
        <dt>Scorer</dt>
        <dd className="mono">{versioned(evaluation.scorer_id ?? scoringPass?.scorer_id, evaluation.scorer_version ?? scoringPass?.scorer_version)}</dd>
        <dt>ScoringPass</dt>
        <dd className="mono">{run.current_scoring_pass_id ?? "未生成"}{scoringPass?.source ? ` · ${scoringPass.source}` : ""}</dd>
        {modelSnapshot.id && (
          <>
            <dt>ModelProfile 快照</dt>
            <dd className="mono" title={typeof (modelSnapshot.profile_hash ?? modelSnapshot.content_hash) === "string"
              ? String(modelSnapshot.profile_hash ?? modelSnapshot.content_hash) : undefined}>
              {modelSnapshot.id} · g{modelSnapshot.generation ?? 1} · {modelSnapshot.lifecycle ?? "legacy"} · {shortHash(modelSnapshot.profile_hash ?? modelSnapshot.content_hash)}
            </dd>
          </>
        )}
        {providerSnapshot.name && (
          <>
            <dt>Provider 快照</dt>
            <dd className="mono" title={typeof providerSnapshot.content_hash === "string" ? providerSnapshot.content_hash : undefined}>
              {providerSnapshot.name} · g{providerSnapshot.generation ?? 1} · {shortHash(providerSnapshot.content_hash)}
            </dd>
          </>
        )}
        {identities.length > 0 && (
          <>
            <dt>模型身份</dt>
            <dd className="mono">
              请求 {fieldSummary(identities, "requested_model")} · 报告 {fieldSummary(identities, "reported_model")} · 实际 {fieldSummary(identities, "resolved_model_identity")}
              {identities.length > 1 ? ` · ${identities.length} cases` : ""}
            </dd>
            <dt>身份策略</dt>
            <dd className="mono">
              {new Set(identities.map((item) => item.identity_policy ?? "report_only")).size === 1
                ? identities[0].identity_policy ?? "report_only"
                : "多种策略"}
              {identities.every((item) => item.policy_passed === true)
                ? " · 通过"
                : identities.some((item) => item.policy_passed === false)
                  ? " · 未通过"
                  : " · 未判定"}
              {` · 判定 ${verdictSummary(identities)}`}
            </dd>
          </>
        )}
      </dl>
    </section>
  );
}
