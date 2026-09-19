"""发布契约一致性检查（review R2-12）。

重新生成 openapi.json 与提交产物比对，漂移即非零退出——公共接口演进
不能只更新 Python 路由。用法：``uv run python scripts/check_openapi.py``。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apps.api.app.main import app  # noqa: E402


def main() -> int:
    generated = json.dumps(
        app.openapi(), ensure_ascii=False, indent=2, sort_keys=True,
    ) + "\n"
    committed_path = ROOT / "api" / "openapi.json"
    committed = committed_path.read_text(encoding="utf-8")
    if generated == committed:
        print("openapi.json is up to date")
        return 0
    print(
        f"{committed_path.relative_to(ROOT)} is stale; regenerate with: make openapi",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
