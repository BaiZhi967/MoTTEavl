const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  preparing: "准备中",
  running: "运行中",
  collecting: "收集中",
  scoring: "评分中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  unsupported: "不支持",
  profile_stale: "配置过期",
};

const STATUS_CLASS: Record<string, string> = {
  completed: "status-completed",
  failed: "status-failed",
  cancelled: "status-cancelled",
  unsupported: "status-failed",
  profile_stale: "status-failed",
  running: "status-running",
  queued: "status-queued",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`status-badge ${STATUS_CLASS[status] ?? ""}`}>{statusLabel(status)}</span>;
}
