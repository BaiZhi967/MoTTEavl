# Trace 保留：显式计划、先归档后裁剪

## 当前边界

Trace 保留默认关闭：`enabled=false`、`retention_days=null`。启用必须在 JSON
配置中同时提供 `enabled=true` 与严格正整数 `retention_days`；没有默认保留天数、
环境变量启用、定时器、启动自动执行或 HTTP 修改接口。本轮 apply 验证仅针对
合成、一次性测试数据库，不代表真实用户数据清理或生产保留验收，也不授权生产操作。

CLI 与 SDK 只在本地执行；`--mode server`（包括 `MOTTE_CLI_MODE=server`）
返回退出码 2，不发远程请求，也不打开本地数据库。PostgreSQL 本地操作仍使用既有
`MOTTE_STORAGE=postgres` 与 `MOTTE_PG_DSN`/`DATABASE_URL` 存储配置。

## 操作流程

先单独初始化/升级测试数据库。plan 不创建缺失数据库、不自动升级 SQLite
旧结构，也不写入业务数据。缺失/旧 schema 必须先在独立维护流程中处理。
不传配置只生成禁用的规范化 JSON 计划：

```sh
motte trace-retention plan --db /tmp/synthetic-runs.db --output /tmp/disabled-plan.json
```

在一次性测试环境中，显式保存配置，例如下面的 7 天仅为示例，绝非默认政策：

```json
{"enabled":true,"retention_days":7}
```

```sh
motte trace-retention plan --db /tmp/synthetic-runs.db \
  --config /tmp/retention.json --output /tmp/trace-plan.json
# 人工检查计划的 store identity、配置、cutoff、Run/seq 范围与摘要后：
motte trace-retention apply --db /tmp/synthetic-runs.db \
  --config /tmp/retention.json --plan /tmp/trace-plan.json \
  --artifacts-root /tmp/synthetic-artifacts --confirm
```

工件根目录必须事先已持久化，且与这个库对应。apply 必须同时提供保存的精确计划、
同一配置、工件目录及 `--confirm`；缺项或未确认拒绝，绝不重新计算并静默执行新计划。
计划包含存储身份的摘要，不含 DSN；配置、状态、revision、保护引用、事件原文或
服务器时间改变会使计划失效。复验使用保存的 cutoff，不因经过时间扩大范围。
精确重复执行已成功计划，会重新验证归档并返回相同收据及 `trimmed_events=0`。
stdout 为 JSON；拒绝输出 stderr JSON 并以 2 退出。

SDK 使用完全相同的类型和函数签名（显式导入不会自动启用）：

```python
from motte_sdk.trace_retention import (
    TraceRetentionConfig, TraceRetentionPlan,
    plan_trace_retention, apply_trace_retention,
)

config = TraceRetentionConfig()  # disabled; no default duration
plan = plan_trace_retention(store, config=config)
# apply_trace_retention(store, artifacts_root, saved_plan, config=explicit_config, confirm=True)
```

## 选择与证据保护

- 仓储在追加时记录服务器写入时间；它不进入公共 Trace payload。迁移前事件的未知
  时间仍为 NULL，不使用 payload 时间回填；未知、近期、受保护或缺失序号均阻断前缀
- 仅考虑 completed/failed/cancelled/unsupported/profile_stale 的连续旧前缀；
  active、needs_review、imported、Baseline/Gate/StatisticalReport、Judge、评分事件
  及其他独立引用均保留。引用枚举失败即拒绝
- 永远保留每个 Run 最高 seq；后续追加使用 max(seq)+1，不复用已裁剪序号
- apply 取得现有 Worker、维护和工件锁后复验；归档文件及目录层级持久化、读回校验
  完成后，才在同一事务发布 receipt 并删除精确前缀。失败只允许无删除或完整提交
- `trace-archives/` 是不可变、永久保留的证据命名空间，包括崩溃遗留的孤立归档。
  GC 不清除它；嵌套工件与仅 hash 引用仍进入统一保护闭包。没有归档到期删除功能

## 历史读取与恢复

事件仓储 `read_window(run_id, after)` 与 `RunService.events_window` 在同一快照读取
实时事件及 receipt 的 `trimmed_through`。SQLite/PG 使用单条查询，Memory 使用共享锁。
当 `after < trimmed_through`，快照返回 `partial=true`，SSE 首先发送
`motte-gap`，其 `next_seq=trimmed_through+1`；即使无事件行返回也不能声称完整。
旧数据中没有 receipt 的首条序号缺口仍保守标记 partial。分页只描述本次游标范围；
SDK 在 gap 补齐、后续完整页和终态对账过程中保留已发现的 partial，不能将整段历史
重新标为完整。公共 Trace schema 与导出格式不增加服务器时间字段。

正常退出释放本次维护 owner；进程崩溃保留维护屏障，不能通过重启自动绕过。
先确认原执行器已退出、核对归档和 receipt 状态，再读取 `motte maintenance status`
返回的原 owner，用 `motte maintenance end --owner OWNER --db ...` 显式解除。
恢复时没有通用 SQL/回调/全局 allow-delete 后门。具体恢复与备份核对见
[备份恢复](backup-restore.md)。

一致备份与 staging 恢复验证归档字节、收据范围/摘要、保留行与完整引用闭包。
恢复只复制归档证据，不把归档重放回活动 Trace；没有 stream-restoration 命令。

## 平台门槛

Memory 可计划/读取，持久 apply 抛 `BackupUnsupported`。SQLite 与 PostgreSQL
证据来自一次性本地合成库；PG 必须实际运行其并发与 dump/restore 测试，skip 不算通过。
Windows Trace archive durability 尚无支持路径：archive apply 以
`safe archive durability primitives are unsupported` 明确 fail-closed；普通读取继续可用。
不能以 POSIX fsync 或合成测试结果宣称 Windows 持久裁剪或生产恢复已验收。

## CLI 历史完整性提示

`motte run-events RUN`（JSONL）与 `--snapshot`（JSON 数组）保持 stdout 仅含真实事件。
local/server 模式发现裁剪收据或缺口时，stderr 输出一条 JSON 元数据：
`{"code":"TRACE_HISTORY_PARTIAL","run_id":"…","after":0,"partial":true,…}`。
已知收据边界以 `trimmed_through` 给出；旧服务器仅返回 partial 时不捏造边界。
即使事件列表为空也保留提示，后续完整分页不能抹去先前缺口。读取成功仍退出 0，
完整性要求严格的消费者必须同时检查此 stderr 元数据。不会在 stdout 插入伪 Trace
事件、gap 事件或伪造时间戳。API snapshot 新增 `trimmed_through`，保留原有相对于
请求 cursor 的 `partial` 语义；CLI 的提示同时标明整个 Run 的历史曾被裁剪。
