import { Fragment, useState, type ReactNode } from "react";
import { Board } from "../board/Board";
import { OutcomeFlap } from "../board/OutcomeFlap";
import { EmptyBoard } from "../board/EmptyBoard";

export interface DrillRow {
  caseId: string;
  outcomeLabel: string;
  outcomeTone: "success" | "error" | "neutral";
  summary: string;
  detail: ReactNode;
}

/** 逐题下钻：板面 + 就地展开（把这一行抽出来看证据，不跳走）。 */
export function CaseDrillTable({ rows }: { rows: DrillRow[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <Board
      label="逐题板面"
      head={
        <>
          <th className="w-[28px]"><span className="sr-only">展开</span></th>
          <th className="w-[220px]">Case</th>
          <th className="w-[120px]">判定</th>
          <th>摘要</th>
        </>
      }
    >
      {rows.map((row) => {
        const expanded = open === row.caseId;
        return (
          <Fragment key={row.caseId}>
            <tr>
              <td className="board-caret">
                <button
                  type="button"
                  className="board-open"
                  aria-expanded={expanded}
                  aria-label={expanded ? "收起 " + row.caseId + " 的详情" : "展开 " + row.caseId + " 的详情"}
                  onClick={() => setOpen(expanded ? null : row.caseId)}
                >
                  <span aria-hidden>{expanded ? "▾" : "▸"}</span>
                </button>
              </td>
              <td className="data board-id">
                <button
                  type="button"
                  className="link run-id"
                  title={row.caseId}
                  aria-expanded={expanded}
                  onClick={() => setOpen(expanded ? null : row.caseId)}
                >
                  {row.caseId}
                </button>
              </td>
              <td><OutcomeFlap label={row.outcomeLabel} tone={row.outcomeTone} /></td>
              <td className="truncate" title={row.summary}>{row.summary}</td>
            </tr>
            {expanded && (
              <tr className="board-detail">
                <td colSpan={4}><div className="drill-detail">{row.detail}</div></td>
              </tr>
            )}
          </Fragment>
        );
      })}
      {rows.length === 0 && (
        <tr>
          <td colSpan={4} style={{ height: "auto", padding: "16px" }}>
            <EmptyBoard reason="暂无记录" next="这个运行还没有可下钻的题目" />
          </td>
        </tr>
      )}
    </Board>
  );
}
