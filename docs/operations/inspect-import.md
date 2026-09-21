# Inspect EvalLog 只读导入（M4-T11）

导入官方 **EvalLog version=2 的完整 JSON 对象**。不运行 task/solver/scorer，不加载插件、pickle 或日志中的命令。二进制 .eval 不直接支持，先用 Inspect 导出包含 samples 的完整 JSON。

## 固定身份与证据

- 必须包含 version=2、plan、eval 和非空 samples；eval.eval_id/run_id/created/model/task 为非空字符串。
- 来源键是 eval_id + run_id，模型/时间从 eval 对象读取；样本键为 id + epoch，同键重复拒绝。
- parser 为 inspect-json-v3。未知 schema version 拒绝；未知非关键顶层字段保留并标 partial。
- 限制为 UTF-8 64MB、100k 样本、每样本64个评分器；不从 aggregate-only 日志制造样本。
- 原始 UTF-8 字节原样冻结到 var/inspect-imports/<import_id>.source 并记录 SHA-256。OS 文件锁串行执行身份核验和记录发布，原子替换登记文件。
- 同来源同内容重导幂等，同来源异内容报 IMPORT_IDENTITY_CONFLICT；改变样本数不能绕过来源冲突。

## 使用和查询

```bash
uv run python -m motte_cli inspect-import path/to/log.json --name my-import --json
```

API 使用 POST /api/v1/inspect/import，body 为 name（可选）与 content（完整 JSON 文本）。返回 import_id、run_id、scoring_pass_id，可通过已有 GET /api/v1/runs/{run_id}、评分历史、报告和 /runs/{run_id}/cases/{case_id}/agent 查看导入内容。

Run 是只读来源归档，**从不进入 queued/Dispatcher**。导入准备中保持 needs_review；样本、原生 ScoreSet 和 completed 终态由已有 ScoringPass CAS 原子发布。相同上传可完成中断的数据库登记，不会调用模型或原生 Inspect。

原生评分保存为 source=inspect-native、metric_id=native.<scorer>；数字值不自动解释为通过，布尔值保留原生通过语义。缺失或非数值评分保留原值并标证据不足。Observation 如实标明平台未验证工具轨迹，原始来源引用和文件 hash 可追溯；导入完成不代表被测任务通过。公共返回脱敏；原始字节导出沿用平台默认关闭的原始 Artifact 导出策略。

## 回退

关闭新的导入入口，保留既有 Run、ScoringPass、源文件与 Artifact。不得删除已经被历史报告引用的原始日志，也不得覆盖历史原生评分。当前导入 Run 的 retry 与平台 rescore 明确拒绝；未来开放重评时必须配置支持导入证据的 evaluator，并生成独立 pass。

官方结构依据：[EvalLog / EvalSpec](https://inspect.aisi.org.uk/reference/inspect_ai.log.html#evalspec)。本地验证：tests/harness/test_inspect_log_import.py、test_inspect_v2_identity.py，覆盖格式、原文、来源冲突及持久查询；不等同于任意 Inspect 版本兼容声明。
