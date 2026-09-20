# Inspect EvalLog 只读导入（M4-T11）

范围：消费**官方 .json EvalLog 完整格式**（单 JSON 对象，`--full` 导出含
`samples`），把样本与原生评分导入平台证据；**不支持 Inspect 执行**（不
运行 task/solver/scorer、不加载 pickle/插件/日志内命令）。

## 格式与限制

- 支持 `inspect-eval-log-v2`（parser `inspect-jsonl-v2`）：官方 `.json`
  EvalLog 对象——顶层 `version` / `plan`（必需），`status` / `created` /
  `model` / `eval` / `results` / `samples`；每个 sample 取
  `id` / `epoch` / `scores`（scorer 名 → {name, value, answer, explanation}）；
- **二进制 `.eval`（msgpack）不支持**：需先用 inspect 导出
  `inspect view --full --format json`（或等价 `--log-format json --full`）；
- summary-only 导出（无 `samples`）→ `AGGREGATE_ONLY` 拒绝（不从聚合
  伪造样本；错误信息提示 `--full`）；
- 上限：64MB / 100k 样本 / 每样本 64 个 scorer；
- 未知顶层字段：记录进 `unknown_fields`、coverage 降 partial；未知
  schema/状态 → 拒绝（fail closed）；
- 原生分数标记 `score_source=inspect-native`（imported），与平台重评
  pass 区分。

## 使用

```bash
# CLI（.json 完整导出）
uv run python -m motte_cli inspect-import path/to/log.json --name my-import --json

# API（受控上传文本）
curl -X POST /api/v1/inspect/import -d '{"name":"my-import","content":"<EvalLog JSON>"}'
```

导入记录落在 `var/inspect-imports/inspect-<hash>.json`，**原始内容整体
冻结**在同目录 `<import_id>.source`（复核/重解析的单一事实源，
`load_import_raw(import_id)` 只读读取）。

幂等与冲突（M4 review R28）：

- `import_id` 由**稳定源身份**派生（eval 任务 + 创建时间 + 模型 + 样本
  数），不随内容微调漂移；
- 同身份同内容重导 → `idempotent=true`（返回既有记录）；
- 同身份**不同内容**（例如同一导出被改分）→ `IMPORT_IDENTITY_CONFLICT`
  显式报错，不静默产生新导入。

导入不进入 Dispatcher（无执行语义）。

## 回退

导入是纯附加证据：删除对应 `var/inspect-imports/inspect-*.json` 与
`inspect-*.source` 即移除登记；不触碰任何运行/评分历史。
