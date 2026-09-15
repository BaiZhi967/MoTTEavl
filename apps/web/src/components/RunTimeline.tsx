import { useMemo, useState } from "react";
import type { TraceEvent } from "../api/client";

const EVENT_LABELS: Record<string, string> = {
  queued: "进入队列",
  preparing: "准备",
  running: "开始运行",
  collecting: "收集结果",
  scoring: "评分",
  completed: "完成",
  failed: "失败",
  cancelled: "取消",
  unsupported: "不支持",
  profile_stale: "配置过期",
  model_response: "模型响应",
  score: "评分结果",
  rescored: "重新评分",
};

function summarize(event: TraceEvent): string {
  switch (event.type) {
    case "model_response":
      return `case=${event.case_id}`;
    case "score":
      return `case=${event.case_id} ${event.passed ? "通过" : "未通过"}`;
    case "cancelled":
      return event.reason ? `原因：${event.reason}` : "";
    case "failed":
    case "unsupported":
      return event.error ? `${event.error.code ?? event.error.type ?? ""} ${event.error.message ?? ""}`.trim() : "";
    default:
      return "";
  }
}

export function RunTimeline({ events }: { events: TraceEvent[] }) {
  const [filter, setFilter] = useState("all");
  const types = useMemo(() => Array.from(new Set(events.map((event) => event.type))), [events]);
  const visible = filter === "all" ? events : events.filter((event) => event.type === filter);

  return (
    <section className="panel">
      <h2>运行时间线</h2>
      <label>
        事件过滤：
        <select value={filter} onChange={(change) => setFilter(change.target.value)} aria-label="事件过滤">
          <option value="all">全部（{events.length}）</option>
          {types.map((type) => (
            <option key={type} value={type}>
              {EVENT_LABELS[type] ?? type}
            </option>
          ))}
        </select>
      </label>
      <ol className="timeline">
        {visible.map((event) => (
          <li key={`${event.seq}-${event.type}`} className={`event event-${event.type}`}>
            <span className="seq">#{event.seq}</span>
            <span className="type">{EVENT_LABELS[event.type] ?? event.type}</span>
            {summarize(event) && <span className="detail">{summarize(event)}</span>}
          </li>
        ))}
        {visible.length === 0 && <li className="empty">暂无事件</li>}
      </ol>
    </section>
  );
}
