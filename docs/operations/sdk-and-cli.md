# SDK 与 CLI：local/server 模式路由（M7）

> 协议：`docs/protocols/sdk-and-migration.md` §3（frozen@1）。
> SDK 客户端（`motte_sdk.MotteClient`）契约见协议 §1–§2 与
> `packages/sdk-python/motte_sdk/client.py` 模块 docstring。

## 1. 模式路由

每个支持双模式的命令接受 `--mode {local,server}`；server 模式必填
`--api-url`（Bearer 认证可选 `--api-token`）。

配置优先级（协议 §3）：**命令行参数 > 环境变量 > 默认 local**。

| 配置 | 参数 | 环境变量 | 说明 |
|---|---|---|---|
| 模式 | `--mode local\|server` | `MOTTE_CLI_MODE` | 缺省 local |
| API 根地址 | `--api-url` | `MOTTE_API_URL` | server 模式必填，缺失 → `MODE_CONFIG_MISSING`（退出 2） |
| Bearer token | `--api-token` | `MOTTE_API_TOKEN` | 可选；服务端未设 `MOTTE_API_TOKEN` 时为本地信任模式 |

- **local 模式**：与既有 CLI 行为一致（直连本地 SQLite / `MOTTE_DB_PATH`；
  `--db` 仍强制 SQLite）。
- **server 模式**：**只经 `MotteClient` HTTP**——不打开本地 DB、不构造
  Provider、不启动任务。`--db` 与 server 模式同用 → `MODE_MISMATCH`（退出 2）。
  远端不可达/认证失败/超时 → stderr 单行 JSON 诊断 + 非零退出，
  **绝不静默回退本地**（TRANSPORT 类错误也退出 2）。
- stdout 纪律：成功输出机器可读 JSON（`--pretty` 可缩进；`run-events` 默认
  JSONL 每行一条事件）；诊断只走 stderr。
- local/server 同输入产出同错误码与同 JSON 键集
  （`tests/cli/test_remote_parity.py` 固化；如 `SCENARIO_NOT_FOUND`、
  `RUN_NOT_FOUND` 两模式同码）。

## 2. 双模式命令矩阵

| 命令 | local 实现 | server 实现（MotteClient） |
|---|---|---|
| `run`（创建，支持 `--request-key`） | RunService + prepare_run | `create_run`（POST /runs） |
| `run-list [--status]` | store.runs.list | `list_runs` |
| `run-get RUN_ID` | RunService.get_run | `get_run` |
| `run-cancel RUN_ID [--reason]` | RunService.cancel | `cancel_run` |
| `run-retry RUN_ID` | RunService.retry | `retry_run` |
| `run-events RUN_ID [--after N] [--snapshot]` | events_after | `run_events_snapshot` |
| `run-wait RUN_ID [--timeout S] [--poll S]` | 本地轮询 | `wait_for_run` |
| `run-report RUN_ID [--scoring-pass-id]` | 本地报告装配 | `run_report` |
| `experiment preview\|create\|status\|cancel` | ExperimentService | `experiment_*` |
| `compare` | ComparisonService | `compare` |
| `baseline create\|list\|select\|default` | ComparisonService | `create_baseline` / `list_baselines` / `set_default_baseline` / GET |
| `gate policy-publish\|evaluate\|result\|export` | ComparisonService | `publish_gate_policy` / `evaluate_gate_versioned` / `get_gate_result` / `export_gate` |
| `regression` | ComparisonService | `classify_regression` |

`experiment retry-cell` 无远端入口，local-only（server 模式 → `LOCAL_ONLY_COMMAND`）。

### 仅 local 的命令（协议 §3）

涉及宿主文件或凭据，**绝不远程执行**：`backup` / `restore` / `restore-guard` /
`maintenance` / `gc` / `import` / `cleanup-artifacts` / `credentials` / `doctor` /
`benchmark download|import`。这些命令带 `--mode server` 时直接退出 2
（`LOCAL_ONLY_COMMAND`）。

## 3. run-wait 语义

- 轮询直到终态；`needs_review` 也是终态（需用户处理，不自动 retry），退出 0
  并打印 Run JSON。
- `--timeout SECONDS` 超时 → 退出 2，错误码 `TIMEOUT`（附最后已知 status）；
  **只停止等待，绝不暗中 cancel 远端 Run**（协议 §1.4）。
- Ctrl-C（KeyboardInterrupt）→ 退出 4，错误码 `CANCELLED_BY_USER`。

## 4. 幂等创建（`--request-key`，协议 §1.3）

```
# local：canonical hash + motte_request_keys 注册表 + 确定性 run id
motte run --spec run.json --db var/runs.db --request-key payment-safe-1
# server：同一语义由服务端收口（POST /runs body.request_key）
motte run --spec run.json --mode server --api-url http://127.0.0.1:8000 \
    --request-key payment-safe-1
```

同 key 同 body → 返回**同一 Run**（重放响应带 `idempotent_replay: true`）；
同 key 异 body → `REQUEST_KEY_CONFLICT`（退出 2）。local 实现
（`motte_cli.idempotent_create`）与 API 幂等块逐行镜像：canonical hash 覆盖
**原始请求体**（scenario_version + 原始 manifest + 原始 case_ids），run_id 由
`sha256("motte-request-key:" + key)` 前 32 位确定性派生——因此同一 key 在两种
模式下得到同一 run id。

## 5. 退出码（沿用 M6 冻结）

| 退出码 | 语义 |
|---|---|
| 0 | 成功（含 `run-wait` 到达终态、`needs_review`） |
| 1 | `gate evaluate` 决策 quality_fail |
| 2 | 用法/契约/not-found/`TIMEOUT`/模式错误（`MODE_MISMATCH`、`MODE_CONFIG_MISSING`、`LOCAL_ONLY_COMMAND`、`TRANSPORT`、`REQUEST_KEY_CONFLICT`、`CONFIRM_REQUIRED`…） |
| 3 | execution_error（含远端 5xx `SERVER_ERROR`、`BACKUP_INCOMPLETE`、`RESTORE_INCOMPLETE`） |
| 4 | 用户取消（`CANCELLED_BY_USER`） |
| 5 | `compare` not_comparable / `gate evaluate` insufficient·not_comparable |
| 6 | `gate evaluate` safety_block |

失败输出统一为 stderr 单行 `{"error": {"code", "message", ...}}`。

## 6. server 模式安全注意

- 远程部署（非 loopback）必须：反向代理 TLS + 服务端 `MOTTE_API_TOKEN` +
  `MOTTE_ALLOWED_HOSTS`（协议 §10）；CLI 侧对应 `--api-token` /
  `MOTTE_API_TOKEN`。执行类 API 永不无认证裸奔公网。
- token 只经 `Authorization: Bearer` 头传输；SDK `follow_redirects=False`，
  重定向视为 TransportError，不自动跟随（防认证头泄漏到其他 host）。
- 凭据与完整请求 body 不进任何日志或异常文本（`motte_trace.redaction` 复用）。

## 7. 运维命令（local-only）

```
# 一致备份（维护屏障 + 引用制工件校验 + Manifest v2）；flag-less = legacy 在线备份
motte backup --consistent --target var/backups --db var/runs.db --artifacts-root var/artifacts

# staging 恢复（全新目录、不触碰线上库）→ 恢复守卫阻止 Worker 领取
motte restore --source var/backups --staging var/staging-restore
motte restore-guard status --db var/staging-restore/runs.db
motte restore-guard clear --yes --db var/staging-restore/runs.db

# 维护屏障（备份/GC 窗口）
motte maintenance begin --reason "nightly backup" --db var/runs.db
motte maintenance status --db var/runs.db
motte maintenance end --db var/runs.db

# GC：默认 dry-run；apply 必须显式确认（自动持维护屏障 + tombstone 审计）
motte gc plan --artifacts-root var/artifacts --ttl-days 90 --db var/runs.db
motte gc apply --confirm --artifacts-root var/artifacts --db var/runs.db

# 历史迁移：dry-run → apply（apply 前复验来源包 hash）→ 受限回退
motte import plan --source legacy-export/ --db var/runs.db
motte import apply --source legacy-export/ --operator ops@example.com \
    --artifacts-root var/artifacts --db var/runs.db
motte import rollback --import-id imp-accept-0001 --confirm --operator ops@example.com \
    --artifacts-root var/artifacts --db var/runs.db
```

`cleanup-artifacts` 保留为 legacy TTL 清理器（`--help` 已指向 `motte gc`）。

## 8. 示例

```
# server 模式端到端（执行仍在远端 Worker）
motte run --spec run.json --mode server --api-url http://127.0.0.1:8000 \
    --api-token "$MOTTE_API_TOKEN"
motte run-wait run-abc123 --mode server --api-url http://127.0.0.1:8000 --timeout 600
motte run-events run-abc123 --mode server --api-url http://127.0.0.1:8000
motte run-report run-abc123 --mode server --api-url http://127.0.0.1:8000

# 环境变量方式（CI / 脚本）
export MOTTE_CLI_MODE=server MOTTE_API_URL=http://127.0.0.1:8000 MOTTE_API_TOKEN=...
motte run-list --status completed
motte gate evaluate --policy my-policy@1 --run run-abc123
```
