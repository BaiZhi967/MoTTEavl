import { useEffect, useState } from "react";
import { getRuns } from "../api/client";

/** 会话心跳：全站「进行中」的 Run 数（DESIGN.md 3.1 顶栏右区）。
 *  读不到就返回 null 交给调用方写「未知」——不填 0、不伪造（DESIGN.md 9）。
 *  页面不可见时不发请求，回到前台由下一个周期自然接上。 */
const POLL_MS = 10000;

export function useSessionActivity(): number | null {
  const [running, setRunning] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      try {
        const payload = await getRuns("running");
        if (alive) setRunning(payload.items.length);
      } catch {
        if (alive) setRunning(null);
      }
    };
    void tick();
    const timer = setInterval(() => void tick(), POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  return running;
}
