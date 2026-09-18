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

function identityLabel(identities: Record<string, any>[]): string {
  const labels = identities.map((identity) => [
    identity.requested_model,
    identity.reported_model,
    identity.resolved_model_identity,
  ].filter((value) => typeof value === "string" && value).join(" → ") || "未报告");
  const uniqueLabels = [...new Set(labels)];
  const verdicts = new Set(identities.map((item) => item.identity_policy_result).filter(Boolean));
  const verdict = verdicts.size === 1 ? [...verdicts][0] : verdicts.size > 1 ? "多种结果" : "not_evaluated";
  return `${uniqueLabels.join(" | ")} · ${verdict}${identities.length > 1 ? ` · ${identities.length} cases` : ""}`;
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
            <dd className="mono">{modelSnapshot.id} · g{modelSnapshot.generation ?? 1} · {modelSnapshot.lifecycle ?? "legacy"} · {modelSnapshot.profile_hash ?? modelSnapshot.content_hash}</dd>
          </>
        )}
        {providerSnapshot.name && (
          <>
            <dt>Provider 快照</dt>
            <dd className="mono">{providerSnapshot.name} · g{providerSnapshot.generation ?? 1} · {providerSnapshot.content_hash}</dd>
          </>
        )}
        {identities.length > 0 && (
          <>
            <dt>模型身份</dt>
            <dd className="mono">{identityLabel(identities)}</dd>
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
            </dd>
          </>
        )}
      </dl>
    </section>
  );
}
