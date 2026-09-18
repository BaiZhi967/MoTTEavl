import { Fragment, useState, type ReactNode } from "react";

export interface DrillRow {
  caseId: string;
  outcomeLabel: string;
  outcomeTone: "success" | "error" | "neutral";
  summary: string;
  detail: ReactNode;
}

export function CaseDrillTable({ rows }: { rows: DrillRow[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <table className="drill-table">
      <thead>
        <tr>
          <th>Case</th>
          <th>判定</th>
          <th>摘要</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <Fragment key={row.caseId}>
            <tr>
              <td>
                <button type="button" className="link" onClick={() => setOpen(open === row.caseId ? null : row.caseId)} aria-expanded={open === row.caseId}>
                  {row.caseId}
                </button>
              </td>
              <td><span className={`status-badge status-tone-${row.outcomeTone}`}>{row.outcomeLabel}</span></td>
              <td className="muted">{row.summary}</td>
            </tr>
            {open === row.caseId && (
              <tr className="drill-detail-row">
                <td colSpan={3}><div className="drill-detail">{row.detail}</div></td>
              </tr>
            )}
          </Fragment>
        ))}
        {rows.length === 0 && (
          <tr><td colSpan={3} className="empty">暂无记录</td></tr>
        )}
      </tbody>
    </table>
  );
}
