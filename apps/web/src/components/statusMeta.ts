// 状态语义的唯一来源（DESIGN.md 第 3 节）。
// 徽章、时间线、过滤下拉一律从这里取标签与语气，禁止在组件里手写状态颜色。

export type Tone = "success" | "error" | "info" | "warning" | "neutral";

/**
 * 状态归属：run = Run 状态（运行总览过滤下拉只列这一类）；
 * step = 步骤 / checkpoint 状态；resource = 资源校验与校准状态。
 * 缺省视为 run：既有 10 种运行状态不重复声明。
 */
export type StatusScope = "run" | "step" | "resource";

export interface StatusMetaEntry {
  label: string;
  tone: Tone;
  scope?: StatusScope;
}

export const STATUS_META: Record<string, StatusMetaEntry> = {
  // Run 状态（10 种收敛到 5 种语气，DESIGN.md 第 3 节）
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
  // M5 步骤 / checkpoint 状态：未知结果不得显示成成功
  pending: { label: "待执行", tone: "neutral", scope: "step" },
  skipped: { label: "已跳过", tone: "neutral", scope: "step" },
  unknown: { label: "未知", tone: "warning", scope: "step" },
  cleanup_failed: { label: "清理失败", tone: "error", scope: "step" },
  // M5 资源校验 / 校准状态：未运行与能力不可用必须可见，不能被隐藏
  passed: { label: "通过", tone: "success", scope: "resource" },
  not_run: { label: "未运行", tone: "neutral", scope: "resource" },
  unavailable: { label: "能力不可用", tone: "warning", scope: "resource" },
  calibrated: { label: "已校准", tone: "success", scope: "resource" },
  experimental: { label: "实验性", tone: "warning", scope: "resource" },
};

/** 运行总览的状态过滤来源：只列 Run 状态，步骤 / 资源状态不进入 Run 过滤器。 */
export const STATUS_ORDER = Object.entries(STATUS_META)
  .filter(([, meta]) => (meta.scope ?? "run") === "run")
  .map(([status]) => status);

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
  // M5 场景步骤 / fixture 事件
  step_started: "步骤开始",
  step_finished: "步骤结束",
  checkpoint_frozen: "checkpoint 冻结",
  fixture_prepared: "fixture 已准备",
  fixture_cleanup_failed: "fixture 清理失败",
};

const DATA_EVENT_TONES: Record<string, Tone> = {
  model_response: "neutral",
  case_call_failed: "error",
  score: "success",
  scoring_pass_created: "neutral",
  rescored: "info",
  step_started: "info",
  step_finished: "neutral",
  checkpoint_frozen: "neutral",
  fixture_prepared: "neutral",
  fixture_cleanup_failed: "error",
};

export function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? type;
}

export function eventTone(type: string): Tone {
  if (STATUS_META[type]) return STATUS_META[type].tone;
  return DATA_EVENT_TONES[type] ?? "neutral";
}
