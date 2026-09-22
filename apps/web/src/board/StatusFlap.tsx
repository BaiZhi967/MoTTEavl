import { useEffect, useRef, useState } from "react";
import { statusLabel, statusTone } from "../components/statusMeta";

/** 翻牌格：板面的状态列（REDESIGN-PLAN.md §6）。
 *
 * 状态词表是封闭的——标签只从 statusMeta 的单一来源取，禁止在组件里手写状态文案或颜色。
 * 只有进行中（info 语气）呼吸；终态恒亮；unknown 视为需人工处理，不装成成功。
 * 状态真变化时播放一次翻牌（全站唯一的签名动效），首次渲染不播，避免整块板面一起闪。 */
export function StatusFlap({ status }: { status: string }) {
  const tone = statusTone(status);
  const label = statusLabel(status);
  const live = tone === "info";

  const previous = useRef<string | null>(null);
  const [changing, setChanging] = useState(false);

  useEffect(() => {
    const isChange = previous.current !== null && previous.current !== status;
    previous.current = status;
    if (!isChange) return undefined;
    setChanging(true);
    const timer = window.setTimeout(() => setChanging(false), 240);
    return () => window.clearTimeout(timer);
  }, [status]);

  const className = ["flap", live ? "flap-live" : "", changing ? "flap-changing" : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <span className={className} data-tone={tone}>
      <span className="flap-dot" aria-hidden />
      <span className="flap-word">{label}</span>
    </span>
  );
}
