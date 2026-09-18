// 状态语义的唯一来源（DESIGN.md 第 3 节）。
// 徽章、时间线、过滤下拉一律从这里取标签与语气，禁止在组件里手写状态颜色。

export type Tone = "success" | "error" | "info" | "warning" | "neutral";

export const STATUS_META: Record<string, { label: string; tone: Tone }> = {
  queued: { label: "排队中", tone: "neutral" },
  preparing: { label: "准备中", tone: "info" },
  running: { label: "运行中", tone: "info" },
  collecting: { label: "收集中", tone: "info" },
  scoring: { label: "评分中", tone: "info" },
  completed: { label: "已完成", tone: "success" },
  failed: { label: "失败", tone: "error" },
  cancelled: { label: "已取消", tone: "neutral" },
  unsupported: { label: "不支持", tone: "warning" },
  profile_stale: { label: "配置过期", tone: "warning" },
  needs_review: { label: "需人工复核", tone: "warning" },
};

export const STATUS_ORDER = Object.keys(STATUS_META);

export function statusLabel(status: string): string {
  return STATUS_META[status]?.label ?? status;
}

export function statusTone(status: string): Tone {
  return STATUS_META[status]?.tone ?? "neutral";
}

// 时间线事件是动词短语，与状态徽章的标签有意不同（如「开始运行」vs「运行中」）。
const EVENT_LABELS: Record<string, string> = {
  queued: "进入队列",
  preparing: "准备",
  running: "开始运行",
  collecting: "收集结果",
  scoring: "评分",
  completed: "完成",
  failed: "失败",
  cancelled: "取消",
  unsupported: "不支持",
  profile_stale: "配置过期",
  needs_review: "需人工复核",
  model_response: "模型响应",
  case_call_failed: "调用失败",
  score: "评分结果",
  scoring_pass_created: "评分批次已创建",
  rescored: "重新评分",
};

const DATA_EVENT_TONES: Record<string, Tone> = {
  model_response: "neutral",
  case_call_failed: "error",
  score: "success",
  scoring_pass_created: "neutral",
  rescored: "info",
};

export function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? type;
}

export function eventTone(type: string): Tone {
  if (STATUS_META[type]) return STATUS_META[type].tone;
  return DATA_EVENT_TONES[type] ?? "neutral";
}
