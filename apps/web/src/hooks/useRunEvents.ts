import { useEffect, useState } from "react";
import {
  fetchRunEventsSnapshot,
  subscribeRunEvents,
  type TraceEvent,
} from "../api/client";

const TERMINAL_STATUSES = ["completed", "failed", "cancelled", "unsupported", "profile_stale", "needs_review"];

export function isTerminal(status: string | null): boolean {
  return status != null && TERMINAL_STATUSES.includes(status);
}

/** 去重统计已完成的 case 数（成功响应与失败调用都算已完成）。 */
export function countDone(events: TraceEvent[]): number {
  const done = new Set<string>();
  for (const event of events) {
    const payload: Record<string, any> = event.payload ?? (event as unknown as Record<string, any>);
    if ((event.type === "model_response" || event.type === "case_call_failed") && typeof payload.case_id === "string") {
      done.add(payload.case_id);
    }
  }
  return done.size;
}

export interface RunEventsState {
  events: TraceEvent[];
  status: string | null;
  /** true 表示事件流出现过缺口且已用持久查询补齐（协议 sdk-and-migration §2）。 */
  partial: boolean;
}

/**
 * 订阅运行事件流（协议 §7）：显式 after 游标 + seq 去重 + motte-gap 时用
 * events/snapshot 补齐并标记 partial；终态后关闭流。组件消费接口保持向后兼容
 * （新增 partial 字段为增量）。
 */
export function useRunEvents(runId: string): RunEventsState {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [partial, setPartial] = useState(false);

  useEffect(() => {
    setEvents([]);
    setStatus(null);
    setPartial(false);
    const seen = new Set<number>();
    let lastSeq = 0;
    let closed = false;

    const append = (incoming: TraceEvent[]) => {
      if (closed || incoming.length === 0) return;
      const fresh = incoming
        .filter((event) => typeof event.seq === "number" && !seen.has(event.seq))
        .sort((a, b) => a.seq - b.seq);
      if (fresh.length === 0) return;
      for (const event of fresh) seen.add(event.seq);
      lastSeq = Math.max(lastSeq, fresh[fresh.length - 1].seq);
      setEvents((current) => [...current, ...fresh]);
      const terminalEvent = fresh.find((event) => {
        const payload: Record<string, any> =
          event.payload ?? (event as unknown as Record<string, any>);
        return typeof payload.status === "string" && isTerminal(payload.status);
      });
      if (terminalEvent) {
        const payload: Record<string, any> =
          terminalEvent.payload ?? (terminalEvent as unknown as Record<string, any>);
        setStatus(payload.status);
      }
    };

    const unsubscribe = subscribeRunEvents(
      runId,
      {
        onEvent: (event) => {
          append([event]);
          const payload: Record<string, any> =
            event.payload ?? (event as unknown as Record<string, any>);
          if (typeof payload.status === "string") setStatus(payload.status);
          if (isTerminal(payload.status)) closed = true;
        },
        onGap: async (gap) => {
          // 事件段被清理：用持久查询把 seq > gap.after 的现存事件补回来，
          // 补不回的部分如实标记 partial，不假装完整。
          setPartial(true);
          try {
            const snapshot = await fetchRunEventsSnapshot(runId, gap.after);
            append(snapshot.events);
            if (typeof snapshot.run_status === "string") setStatus(snapshot.run_status);
          } catch {
            // 快照也失败时保持 partial，等浏览器自动重连再试
          }
        },
      },
      { after: 0 },
    );
    return unsubscribe;
  }, [runId]);

  return { events, status, partial };
}
