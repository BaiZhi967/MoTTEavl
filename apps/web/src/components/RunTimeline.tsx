import { useMemo, useState } from "react";
import type { TraceEvent } from "../api/client";
import { formatClock } from "./runFormat";
import { eventLabel, eventTone } from "./statusMeta";

function summarize(event: TraceEvent): string {
  const payload: Record<string, any> = event.payload ?? (event as unknown as Record<string, any>);
  switch (event.type) {
    case "model_response":
      return "case=" + payload.case_id;
    case "case_call_failed":
      return "case=" + payload.case_id;
    case "score":
      return "case=" + payload.case_id + " " + (payload.passed ? "通过" : "未通过");
    case "cancelled":
      return payload.reason ? "原因：" + payload.reason : "";
    case "scoring_pass_created":
      return [payload.scorer_id, payload.scorer_version].filter(Boolean).join(" @ ");
    case "needs_review":
      return Array.isArray(payload.attempt_ids) ? "不确定调用 " + payload.attempt_ids.length + " 个" : "";
    case "failed":
    case "unsupported":
      return payload.error
        ? String(payload.error.code ?? payload.error.type ?? "") + " " + String(payload.error.message ?? "")
        : "";
    default:
      return "";
  }
}

/** 转录面（REDESIGN-PLAN.md §5 P4）：事件与日志是一条连续、可搜索、键盘优先的文本面，
 *  不装在卡片壳里。层级靠缩进与整宽横线——这是从"午夜磷光终端"留下的那条纪律。 */
export function RunTimeline({ events, embedded = false }: { events: TraceEvent[]; embedded?: boolean }) {
  const [filter, setFilter] = useState("all");
  const [grep, setGrep] = useState("");
  const types = useMemo(() => Array.from(new Set(events.map((event) => event.type))), [events]);

  const needle = grep.trim().toLowerCase();
  const visible = events.filter((event) => {
    if (filter !== "all" && event.type !== filter) return false;
    if (!needle) return true;
    return (eventLabel(event.type) + " " + summarize(event) + " " + event.type).toLowerCase().includes(needle);
  });

  return (
    <section className={embedded ? undefined : "panel"}>
      <div className="panel-head">
        <h2 className={embedded ? "embed-title" : undefined}>运行时间线</h2>
        <div className="panel-head-actions">
          <div className="inline-field">
            <label className="field-label" htmlFor="timeline-grep">搜索</label>
            <input
              id="timeline-grep"
              className="control"
              value={grep}
              onChange={(change) => setGrep(change.target.value)}
              placeholder="事件关键词 / case id"
              aria-label="搜索事件"
            />
          </div>
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
        </div>
      </div>
      <ol className="timeline transcript">
        {visible.map((event) => (
          <li key={event.seq + "-" + event.type} className={"event event-tone-" + eventTone(event.type)}>
            <span className="seq">#{event.seq}</span>
            {event.recorded_at ? <span className="time">{formatClock(event.recorded_at)}</span> : <span className="time">—</span>}
            <span className="type">{eventLabel(event.type)}</span>
            <span className="detail">{summarize(event)}</span>
          </li>
        ))}
        {visible.length === 0 && (
          <li className="empty">
            {events.length === 0 ? "暂无事件" : "没有匹配「" + grep + "」的事件"}
          </li>
        )}
      </ol>
    </section>
  );
}
