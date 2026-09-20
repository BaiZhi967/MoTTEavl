import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowClockwiseIcon,
  CheckCircleIcon,
  CircleIcon,
  PaperPlaneTiltIcon,
  ProhibitIcon,
  RocketIcon,
} from "@phosphor-icons/react";
import {
  createRun,
  getRuns,
  getRuntimes,
  getRuntimeProfiles,
  getScenarios,
  publishRuntimeProfile,
  publishRuntimes,
  type RuntimeCatalogItem,
  type RuntimeProfileRecord,
  type RunRecord,
} from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { statusLabel } from "../../components/statusMeta";
import { suiteRoutes } from "../registry";

export const ROUTES = suiteRoutes("runtimes");

const MODEL_CONTROL_LABELS: Record<string, string> = {
  "platform-controlled": "平台控制模型",
  "runner-configured": "运行器配置模型",
  "externally-managed": "外部管理认证",
};

function ReadinessFlag({ ready, label }: { ready: boolean; label: string }) {
  const Icon = ready ? CheckCircleIcon : CircleIcon;
  return (
    <span className={`readiness-flag ${ready ? "ok" : "pending"}`}>
      <Icon size={14} weight="bold" aria-hidden />
      {label}
    </span>
  );
}

function Panel({ title, children, actions }: { title: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <section className="panel detail" aria-label={title}>
      <div className="panel-head">
        <h2>{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

/* ------------------------------------------------------------------ 目录页 */

export function HarnessOperate() {
  const navigate = useNavigate();
  const [items, setItems] = useState<RuntimeCatalogItem[] | null>(null);
  const [profiles, setProfiles] = useState<RuntimeProfileRecord[] | null>(null);
  const [scenarios, setScenarios] = useState<{ name: string; version: string; suite?: string }[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [publishNote, setPublishNote] = useState<string | null>(null);

  // 创建表单状态（R20：预检 + 提交 + 监控的用户链路）。
  const [scenario, setScenario] = useState("");
  const [runtime, setRuntime] = useState("");
  const [profileRef, setProfileRef] = useState("");
  const [nativeJson, setNativeJson] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [runtimePayload, profilePayload, scenarioPayload] = await Promise.all([
        getRuntimes(),
        getRuntimeProfiles().catch(() => ({ items: [], total: 0 })),
        getScenarios().catch(() => ({ items: [] })),
      ]);
      setItems(runtimePayload.items);
      setProfiles(profilePayload.items);
      const agentScenarios = scenarioPayload.items
        .filter((item: any) => !item.suite || item.suite === "agent-tasks")
        .map((item: any) => ({ name: item.name, version: item.version, suite: item.suite }));
      setScenarios(agentScenarios);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onPublish = useCallback(async () => {
    setPublishing(true);
    setPublishNote(null);
    try {
      const result = await publishRuntimes();
      setPublishNote(`已发布 ${result.total} 个规范版本（幂等）`);
      await load();
    } catch (cause) {
      setPublishNote(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPublishing(false);
    }
  }, [load]);

  const publishedRuntimes = useMemo(
    () => (items ?? []).filter((item) => item.published),
    [items],
  );
  const runtimeProfiles = useMemo(
    () => (profiles ?? []).filter((item) => item.runtime === runtime),
    [profiles, runtime],
  );
  const agentScenarios = scenarios ?? [];

  const submitCreate = (form: React.FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setCreateError(null);
    if (!scenario || !runtime) {
      setCreateError("请选择场景与 runtime（runtime 需已发布）");
      return;
    }
    let profile: Record<string, unknown>;
    if (profileRef) {
      const record = runtimeProfiles.find((item) => `${item.name}@${item.version}` === profileRef);
      if (!record) {
        setCreateError("所选 profile 不属于当前 runtime");
        return;
      }
      profile = {
        runtime: record.runtime,
        native_settings: record.native_settings,
        workspace: record.workspace,
        budgets: record.budgets,
        credential_refs: record.credential_refs,
      };
    } else {
      if (!nativeJson.trim()) {
        setCreateError("内联配置不能为空（至少包含 model 字段）");
        return;
      }
      try {
        profile = {
          runtime: `${runtime}@1`,
          native_settings: JSON.parse(nativeJson),
        };
      } catch (cause) {
        setCreateError(`原生设置 JSON 无法解析：${String(cause)}`);
        return;
      }
    }
    setCreating(true);
    createRun({
      scenario_version: scenario,
      // 预检在创建时由后端 resolve_manifest 完成：schema/工具/版本任一
      // 不符都会在提交处失败，而不是静默忽略。
      manifest: {
        runtime: `${runtime}@1`,
        runtime_profile: profile,
        runtime_accept_unenforced_tools: true,
      },
    })
      .then((run) => navigate(`/runs/${run.id}/monitor`))
      .catch((cause) => setCreateError(String(cause)))
      .finally(() => setCreating(false));
  };

  return (
    <div className="page stack">
      <header className="page-head">
        <h1>外部 Runtime</h1>
        <p className="page-sub">
          Pi / Claude CLI / Codex CLI 的固定上游版本与分层就绪；执行、事件、产物与评分沿用平台 Run 链路。
        </p>
      </header>
      <Panel
        title="Runtime 目录"
        actions={
          <div className="panel-actions">
            {publishNote && <span className="hint">{publishNote}</span>}
            <button type="button" className="ghost" onClick={() => void load()}>
              <ArrowClockwiseIcon size={14} weight="bold" aria-hidden /> 刷新
            </button>
            <button type="button" disabled={publishing} onClick={() => void onPublish()}>
              <RocketIcon size={14} weight="bold" aria-hidden />
              {publishing ? "发布中…" : "发布规范版本"}
            </button>
          </div>
        }
      >
        {error && <p className="error-text" role="alert">{error}</p>}
        {!items && !error && <p className="empty-state">加载中…</p>}
        {items && items.length === 0 && <p className="empty-state">目录为空</p>}
        {items && items.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>Runtime</th>
                <th>类型 / 传输</th>
                <th>固定上游</th>
                <th>模型控制</th>
                <th>就绪分层</th>
                <th>发布</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={`${item.name}@${item.version}`}>
                  <td>
                    <strong>{item.name}@{item.version}</strong>
                    {item.interactive && <span className="hint"> · 交互</span>}
                  </td>
                  <td>{item.kind} / {item.transport}</td>
                  <td>{item.upstream_version}</td>
                  <td>{MODEL_CONTROL_LABELS[item.model_control ?? ""] ?? item.model_control}</td>
                  <td>
                    <div className="stack tight">
                      <ReadinessFlag ready={item.readiness.installed} label="已安装" />
                      <ReadinessFlag ready={item.readiness.protocol_ready} label="协议就绪" />
                      <ReadinessFlag ready={item.readiness.execution_ready} label="可执行" />
                    </div>
                  </td>
                  <td>{item.published ? "已发布" : "未发布"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {items && (
          <details className="reasons">
            <summary>各层未就绪原因</summary>
            <ul>
              {items.flatMap((item) =>
                Object.entries(item.readiness.reasons).map(([level, reason]) => (
                  <li key={`${item.name}-${level}`}>
                    <strong>{item.name}</strong> · {level}：{reason}
                  </li>
                )),
              )}
            </ul>
          </details>
        )}
        <p className="hint">
          交互通道（codex-app-server）的命令消费者接通前，消息入口保持关闭；batch Run 用下方表单发起。
        </p>
      </Panel>

      <Panel title="创建 Runtime Run">
        {createError && <p className="error-text" role="alert">{createError}</p>}
        <form onSubmit={submitCreate} aria-label="创建 runtime run">
          <label>
            场景（agent-tasks）
            <select className="control" value={scenario} onChange={(change) => setScenario(change.target.value)}>
              <option value="">选择场景…</option>
              {agentScenarios.map((item) => (
                <option key={`${item.name}@${item.version}`} value={`${item.name}@${item.version}`}>
                  {item.name}@{item.version}
                </option>
              ))}
            </select>
          </label>
          <label>
            Runtime（已发布）
            <select className="control" value={runtime} onChange={(change) => {
              setRuntime(change.target.value);
              setProfileRef("");
            }}>
              <option value="">选择 runtime…</option>
              {publishedRuntimes.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}@{item.version}
                </option>
              ))}
            </select>
          </label>
          <label>
            Profile（可选，已发布版本）
            <select className="control" value={profileRef} onChange={(change) => setProfileRef(change.target.value)}>
              <option value="">内联配置…</option>
              {runtimeProfiles.map((item) => (
                <option key={`${item.name}@${item.version}`} value={`${item.name}@${item.version}`}>
                  {item.name}@{item.version}
                </option>
              ))}
            </select>
          </label>
          {!profileRef && (
            <label>
              原生设置 JSON（runner-configured；至少 model 字段）
              <textarea
                className="control mono"
                rows={4}
                value={nativeJson}
                onChange={(change) => setNativeJson(change.target.value)}
                placeholder='{"model":"scripted-1","script":[[{"type":"text","text":"ok"}]]}'
              />
            </label>
          )}
          <button type="submit" className="primary" disabled={creating}>
            <PaperPlaneTiltIcon size={14} weight="bold" aria-hidden />
            {creating ? "提交中…" : "预检并创建"}
          </button>
        </form>
        <p className="hint">
          提交即预检：schema / 工具边界 / pinned 版本任一不符会在创建处失败；创建成功后跳转监控页。
        </p>
      </Panel>

      <RuntimeProfilesPanel profiles={profiles} onPublished={() => void load()} />
    </div>
  );
}

/* ------------------------------------------------------------------ Profile 管理 */

function RuntimeProfilesPanel({
  profiles,
  onPublished,
}: {
  profiles: RuntimeProfileRecord[] | null;
  onPublished: () => void;
}) {
  const [name, setName] = useState("");
  const [runtimeRef, setRuntimeRef] = useState("");
  const [nativeJson, setNativeJson] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = (form: React.FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setError(null);
    if (!name.trim() || !runtimeRef.trim() || !nativeJson.trim()) {
      setError("name、runtime 与原生设置均为必填");
      return;
    }
    let native: Record<string, unknown>;
    try {
      native = JSON.parse(nativeJson);
    } catch (cause) {
      setError(`原生设置 JSON 无法解析：${String(cause)}`);
      return;
    }
    setBusy(true);
    publishRuntimeProfile({
      name: name.trim(),
      version: "1",
      runtime: runtimeRef.trim(),
      native_settings: native,
    })
      .then(() => {
        setName("");
        setNativeJson("");
        onPublished();
      })
      .catch((cause) => setError(String(cause)))
      .finally(() => setBusy(false));
  };

  return (
    <Panel title="Runtime Profile">
      {profiles && profiles.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Profile</th>
              <th>Runtime</th>
              <th>发布时间</th>
            </tr>
          </thead>
          <tbody>
            {profiles.map((item) => (
              <tr key={`${item.name}@${item.version}`}>
                <td><strong>{item.name}@{item.version}</strong></td>
                <td>{item.runtime}</td>
                <td>{item.published_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {profiles && profiles.length === 0 && (
        <p className="empty-state">暂无已发布 profile（不可变版本资源；同名同版重复发布幂等）</p>
      )}
      <form onSubmit={submit} aria-label="发布 runtime profile">
        <label>
          名称
          <input className="control mono" value={name} onChange={(change) => setName(change.target.value)} />
        </label>
        <label>
          Runtime（name@version）
          <input
            className="control mono"
            value={runtimeRef}
            onChange={(change) => setRuntimeRef(change.target.value)}
            placeholder="pi-agent@1"
          />
        </label>
        <label>
          原生设置 JSON（profile 内容）
          <textarea
            className="control mono"
            rows={4}
            value={nativeJson}
            onChange={(change) => setNativeJson(change.target.value)}
            placeholder='{"model":"scripted-1","script":[[{"type":"text","text":"ok"}]]}'
          />
        </label>
        <button type="submit" className="primary" disabled={busy}>
          <RocketIcon size={14} weight="bold" aria-hidden />
          {busy ? "发布中…" : "发布 Profile"}
        </button>
      </form>
    </Panel>
  );
}

/* ------------------------------------------------------------------ 运行页 */

export function HarnessMonitor() {
  const [runs, setRuns] = useState<RunRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getRuns()
      .then((payload) => {
        if (active) {
          setRuns(payload.items.filter((run) => Boolean(run.manifest?.runtime)));
        }
      })
      .catch((cause: unknown) => {
        if (active) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="page stack">
      <header className="page-head">
        <h1>Runtime 运行</h1>
        <p className="page-sub">manifest.runtime 驱动的 Run（Pi / Claude / Codex batch）。</p>
      </header>
      <Panel title="运行列表">
        {error && <p className="error-text" role="alert">{error}</p>}
        {!runs && !error && <p className="empty-state">加载中…</p>}
        {runs && runs.length === 0 && (
          <p className="empty-state">
            <ProhibitIcon size={14} weight="bold" aria-hidden /> 暂无 runtime Run
          </p>
        )}
        {runs && runs.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>Run</th>
                <th>Runtime</th>
                <th>状态</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id}>
                  <td>
                    {/* 详情路由是 /runs/:runId/monitor（R21：不再指向不存在的 /runs/:id） */}
                    <Link to={`/runs/${run.id}/monitor`}>{run.id}</Link>
                  </td>
                  <td>{run.manifest?.runtime ?? "-"}</td>
                  <td>
                    <StatusBadge status={run.status} />
                  </td>
                  <td>{statusLabel(run.status) && run.created_at ? run.created_at : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
