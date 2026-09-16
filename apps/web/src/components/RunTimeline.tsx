import { useMemo, useState } from "react";
import type { TraceEvent } from "../api/client";
import { eventLabel, eventTone } from "./statusMeta";

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

export function RunTimeline({ events, embedded = false }: { events: TraceEvent[]; embedded?: boolean }) {
  const [filter, setFilter] = useState("all");
  const types = useMemo(() => Array.from(new Set(events.map((event) => event.type))), [events]);
  const visible = filter === "all" ? events : events.filter((event) => event.type === filter);

  return (
    <section className={embedded ? undefined : "panel"}>
      <h2 className={embedded ? "embed-title" : undefined}>运行时间线</h2>
      <div className="inline-field">
        <label className="field-label" htmlFor="timeline-filter">事件过滤</label>
        <select
          id="timeline-filter"
          className="control"
          value={filter}
          onChange={(change) => setFilter(change.target.value)}
          aria-label="事件过滤"
        >
          <option value="all">全部（{events.length}）</option>
          {types.map((type) => (
            <option key={type} value={type}>
              {eventLabel(type)}
            </option>
          ))}
        </select>
      </div>
      <ol className="timeline">
        {visible.map((event) => (
          <li key={`${event.seq}-${event.type}`} className={`event event-tone-${eventTone(event.type)}`}>
            <span className="seq">#{event.seq}</span>
            <span className="type">{eventLabel(event.type)}</span>
            {summarize(event) && <span className="detail">{summarize(event)}</span>}
          </li>
        ))}
        {visible.length === 0 && <li className="empty">暂无事件</li>}
      </ol>
    </section>
  );
}
