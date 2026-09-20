import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowClockwiseIcon,
  CheckCircleIcon,
  CircleIcon,
  ProhibitIcon,
  RocketIcon,
} from "@phosphor-icons/react";
import {
  getRuns,
  getRuntimes,
  publishRuntimes,
  type RuntimeCatalogItem,
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
  const [items, setItems] = useState<RuntimeCatalogItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [publishNote, setPublishNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const payload = await getRuntimes();
      setItems(payload.items);
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
          交互通道（codex-app-server）的命令消费者接通前，消息入口保持关闭；batch Run 经
          <Link to="/runs"> 通用 Run 创建</Link>
          （manifest.runtime 字段）发起。
        </p>
      </Panel>
    </div>
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
                    <Link to={`/runs/${run.id}`}>{run.id}</Link>
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
