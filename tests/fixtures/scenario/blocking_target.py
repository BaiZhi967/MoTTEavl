"""受控测试 Target：读 JSON Lines，可被要求"阻塞"以证明真实停止。

测试专用；合成的订单状态只存于本进程内存，不接触任何真实业务系统。
"""
import json
import sys
import time


def main() -> int:
    sys.stdout.write(json.dumps({"ready": True}) + "\n")
    sys.stdout.flush()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        request = json.loads(raw)
        message = str(request.get("message") or "")
        if "block" in message:
            # 故意阻塞并持续产生副作用：只有被真实终止才会停下。
            while True:
                sys.stderr.write("tick\n")
                sys.stderr.flush()
                time.sleep(0.02)
        if "cancel" in message:
            sys.stdout.write(json.dumps({
                "output": "已取消", "termination_reason": "final_answer",
            }) + "\n")
        else:
            sys.stdout.write(json.dumps({
                "output": "请确认", "termination_reason": "final_answer",
            }) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
