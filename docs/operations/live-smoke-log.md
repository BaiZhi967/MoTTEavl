# Live smoke 记录

每次由操作者显式启动的真实 Provider 冒烟都记录一节（`--record docs/operations/live-smoke-log.md` 自动追加，模板如下）。默认测试与 CI 永不发起真实调用；密钥只从环境变量读取，不进入任何记录。

## 模板

```
## <UTC 时间戳> openai-compatible/<model-id>

- base_url: `<endpoint>`
- 状态：completed / failed
- latency_ms: <n>，attempts: <n>，retry_count: <n>
- usage: prompt_tokens=<n> completion_tokens=<n>
- cost: total=<x>（price_table_version=<v>）  # 或 unknown（未提供价格表）
- 错误分类：<network/rate_limit/auth/server/client/protocol>，message: <...>
- 结论：<操作者补充：是否符合预期、价格表是否更新等>
```
