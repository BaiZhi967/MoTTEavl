"""套件无关的执行语义常量（GSM8K 与 Direct LLM 共用）。"""

# 单题失败是否继续跑后续题目：暂态故障继续，认证/客户端（模型或端点写错）/配置/协议/未知错误
# 一律停跑（run 记 failed），避免拿一整批注定失败的真实调用去烧费用。
CONTINUE_ERROR_CLASSES = frozenset({"rate_limit", "server", "timeout", "network"})
