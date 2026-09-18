/** run 展示格式化：ID 截断与时间列（时间戳为本地时区，控制台自用不做时区切换）。 */

export function shortRunId(id: string): string {
  return id.length <= 22 ? id : `${id.slice(0, 13)}…${id.slice(-4)}`;
}

function parseDate(iso?: string | null): Date | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

const pad = (value: number) => String(value).padStart(2, "0");

/** 总览表「开始」列：MM-DD HH:mm。 */
export function formatTimestamp(iso?: string | null): string | null {
  const date = parseDate(iso);
  if (!date) return null;
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 时间线事件列：MM-DD HH:mm:ss（长运行跨天也能对上日历）。 */
export function formatClock(iso?: string | null): string | null {
  const date = parseDate(iso);
  if (!date) return null;
  return `${formatTimestamp(iso)}:${pad(date.getSeconds())}`;
}

/** 耗时列：finished_at - created_at，分级到 h/m/s。 */
export function formatDuration(from?: string | null, to?: string | null): string | null {
  const start = parseDate(from);
  const end = parseDate(to);
  if (!start || !end || end < start) return null;
  const totalSeconds = Math.round((end.getTime() - start.getTime()) / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h${pad(minutes)}m`;
  if (minutes > 0) return `${minutes}m${pad(seconds)}s`;
  return `${seconds}s`;
}
