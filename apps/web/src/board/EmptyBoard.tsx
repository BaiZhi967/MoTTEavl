import type { ReactNode } from "react";

/** 空态：板面没有行时必须写清原因与下一步，不留纯空白（REDESIGN-PLAN.md §8）。 */
export function EmptyBoard({ reason, next }: { reason: string; next?: ReactNode }) {
  return (
    <div className="empty-board">
      <p className="empty-board-reason">{reason}</p>
      {next ? <p className="empty-board-next">{next}</p> : null}
    </div>
  );
}
