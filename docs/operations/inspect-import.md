# Inspect 日志只读导入（M4-T11）

范围：消费固定 schema 的 Inspect eval-log（JSONL），把样本与原生评分
导入平台证据；**不支持 Inspect 执行**（不运行 task/solver/scorer、不
加载 pickle/插件/日志内命令）。

## 格式与限制

- 支持 `inspect-eval-log-v1` 子集：首行运行 header（model/task/eval），
  后续 `{"sample": {id, epoch, scores}}` 与事件行；
- 上限：64MB / 200k 行 / 每样本 64 个 scorer；
- 未知 schema 行 → 拒绝（fail closed）；只有 aggregate（results 无
  sample）→ 拒绝（不从聚合伪造样本）；
- 原生分数标记 `score_source=inspect-native`（imported），与平台重评
  pass 区分。

## 使用

```bash
# CLI
uv run python -m motte_cli inspect-import path/to/log.eval --name my-import --json

# API（受控上传文本）
curl -X POST /api/v1/inspect/import -d '{"name":"my-import","content":"<JSONL 内容>"}'
```

导入记录落在 `var/inspect-imports/inspect-<hash>.json`：内容先 hash
冻结（import_id 由内容派生）；同内容重导幂等（`idempotent=true`），
内容与身份冲突显式报错。导入不进入 Dispatcher（无执行语义）。

## 回退

导入是纯附加证据：删除对应 `var/inspect-imports/inspect-*.json` 即移除
登记；不触碰任何运行/评分历史。
