import { useEffect, useState } from "react";
import { subscribeRunEvents, type TraceEvent } from "../api/client";

const TERMINAL_STATUSES = ["completed", "failed", "cancelled", "unsupported", "profile_stale"];

export function isTerminal(status: string | null): boolean {
  return status != null && TERMINAL_STATUSES.includes(status);
}

/** 去重统计已完成的 case 数（成功响应与失败调用都算已完成）。 */
export function countDone(events: TraceEvent[]): number {
  const done = new Set<string>();
  for (const event of events) {
    if ((event.type === "model_response" || event.type === "case_call_failed") && typeof event.case_id === "string") {
      done.add(event.case_id);
    }
  }
  return done.size;
}

export function useRunEvents(runId: string): { events: TraceEvent[]; status: string | null } {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    setEvents([]);
    setStatus(null);
    const unsubscribe = subscribeRunEvents(runId, {
      onEvent: (event) => {
        setEvents((current) => (current.some((item) => item.seq === event.seq) ? current : [...current, event]));
        if (typeof event.status === "string") {
          setStatus(event.status);
        }
      },
    });
    return unsubscribe;
  }, [runId]);

  return { events, status };
}
