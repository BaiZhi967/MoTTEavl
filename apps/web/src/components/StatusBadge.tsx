import { statusLabel, statusTone } from "./statusMeta";

export { statusLabel } from "./statusMeta";

export function StatusBadge({ status }: { status: string }) {
  return <span className={`status-badge status-tone-${statusTone(status)}`}>{statusLabel(status)}</span>;
}
