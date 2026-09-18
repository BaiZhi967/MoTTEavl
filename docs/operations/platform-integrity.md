# 平台完整性升级与运行说明

本说明覆盖 `0002_platform_integrity` 引入的 Run 并发控制、单执行器、调用不确定性、追加式评分和资源生命周期。它不承诺外部 Provider 调用 exactly-once。

## 升级步骤

1. 停止 API、Worker 和 Celery consumer，确认没有正在执行的付费调用。
2. 按 [备份与恢复](backup-restore.md) 备份数据库和 `var/artifacts`。
3. PostgreSQL 执行 `uv run alembic upgrade head`，确认 head 为 `0002_platform_integrity`。
4. SQLite 在首次打开现有数据库时幂等创建 `case_attempts`、`scoring_passes`、`score_sets`、`run_commands` 及 revision 支撑结构，无需单独迁移命令。
5. 重新生成或核对 API 合约：`make openapi`，然后执行 `make check`。
6. 先启动 API，再只启动一个 Worker。第二个 Worker 无法取得执行锁时会立即退出，不得把它配置为无限快速重启。

旧 `run-N`、旧 TraceEvent 和旧报告仍可读。旧 Run 在兼容读取时投影为 `schema_version=1`、`revision=0`；新写入使用 `run-<uuid>`、`schema_version=2` 和 CAS revision。升级不会重写既有 Provider 响应或历史评分。

## 执行与恢复

所有持久 Run 必须经过 `RunDispatcher`：先原子 claim 为 `preparing`，再按 `manifest.execution` 选择 ExecutionBackend。API 只排队；Worker、Celery 和 CLI replay 共用这条路径。

默认部署是单执行器：

- SQLite 使用数据库旁的 OS 文件锁。
- PostgreSQL 使用 session advisory lock。
- 锁必须先于恢复取得，防止健康 Worker 的中间态被另一进程重排。

每个外部 case 调用先持久化 `CaseAttempt(prepared)`，发出前转为 `dispatching`，落库时与 CaseRun 和 TraceEvent 一起转为 `succeeded` 或 `failed`。重启时：

- 尚未 dispatch 的安全中间态可以重新排队。
- 残留 `dispatching` 说明请求可能已被 Provider 接收；attempt 转为 `indeterminate`，Run 转为 `needs_review`，不会自动再次产生费用。
- 操作者核对 Provider 侧证据后，可显式 retry 创建子 Run；系统不会把该路径描述为 exactly-once。

## 评分与报告

首次评分和每次 rescore 都创建不可变 `ScoringPass` 与 score set。Run 的 `current_scoring_pass_id` 仅选择当前视图，历史 pass 不会被覆盖。

- `GET /api/v1/runs/{id}/scoring-passes` 列出历史 pass。
- `GET /api/v1/runs/{id}/report?scoring_pass_id=...` 读取指定 pass。
- GET 报告不会临时重算分数。
- `POST /api/v1/runs/{id}/rescore` 只读取已持久化结果，不重新调用 subject model。

## 资源与快照

Dataset、Scenario 和 PriceTable 的 `(name, version)` 记录是 insert-only，已发布记录不可覆盖或删除。ModelProfile 生命周期为：

1. `draft`：可编辑，可通过显式 test 做 smoke call，但不能创建正式 Run。
2. `published`：可创建 Run，内容和 hash 固定。
3. `deprecated`：历史快照仍可读，不能用于新 Run。

ProviderConnection 保持可变，每次更新递增 generation；删除会保留不可见 tombstone，重建后 generation 继续递增，旧 CAS token 不会命中新资源。创建 Run 时，ResolvedManifest v2 固定 execution/evaluation/provider 版本及 ModelProfile、ProviderConnection、Dataset、Scenario、PriceTable 快照和内容 hash。普通 retry 复用原快照；`profile_stale` retry 必须重新解析 requested manifest。

## 能力边界

`direct-llm@1` 与 `replay@1` 是当前可用的 ExecutionBackend；`external-benchmark@1` 已保留版本化注册项但标记为 unavailable，会在创建期失败。Provider adapter 只负责模型协议，不等于执行后端。

Pi bridge 默认实现只验证严格 JSONL v1 协议并报告 `execution_ready=false`；prompt 返回 `PI_BACKEND_UNAVAILABLE`，不会 echo 输入或伪造成功。Builtin Agent、Claude/Codex Harness 的协议或二进制探测也不代表已接入 RunDispatcher。API/Web 分别展示 `protocol_ready` 与 `execution_ready`。

RunCommand 已持久化，但没有 backend command consumer。`POST /runs/{id}/messages` 对非交互 backend 返回 409，对尚未接通 consumer 的交互能力返回 501，不会返回虚假的 202 accepted。

## 回退

优先使用 `git revert` 回退应用代码，并保留新表供证据审计。若必须对 PostgreSQL 执行 `uv run alembic downgrade -1`，`case_attempts`、`scoring_passes`、`score_sets` 和 `run_commands` 会被删除，必须先备份；回退后的旧应用也无法理解 `needs_review` 或 ResolvedManifest v2。SQLite 没有就地降级路径，应停服并恢复升级前备份。
