export function RunProgress({ done, total }: { done: number; total: number }) {
  const ratio = total > 0 ? Math.min(1, done / total) : 0;
  return (
    <span className="run-progress">
      <span className="mono">{done}/{total}</span>
      <span className="progress-bar" role="progressbar" aria-valuenow={done} aria-valuemin={0} aria-valuemax={total}>
        <span className="progress-fill" style={{ width: `${Math.round(ratio * 100)}%` }} />
      </span>
    </span>
  );
}
