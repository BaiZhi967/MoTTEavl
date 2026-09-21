# MoTTEavl Release Notes — M7（SDK、历史迁移、备份恢复与发布）

> 状态：draft → RC 信息在最终门禁后回填。版本命名 `0.1.0-m7-rc1`（语义化前缀），
> 正式 `stable_supported` 以支持矩阵证据为准，不因本文件发布而自动成立。

## M7 变更总览

- **类型化同步 SDK（G01-G03）**：`motte_sdk.MotteClient`（httpx、零副作用导入、
  惰性能力握手、协议 §1.4 重试矩阵、三段超时、`follow_redirects=False`）；
  Run/实验/比较/基线/门禁/导出全链路方法；错误类型化（422 永不自动重试；
  认证/冲突/能力缺失分类明确）。
- **幂等创建**：`POST /runs` 支持 `request_key`（同 key 同 body 幂等返回同一
  Run；同 key 异 body 409 `REQUEST_KEY_CONFLICT`）；服务端确定性 run_id +
  持久注册表（`motte_request_keys`，SQLite/PG/内存一致）。
- **SSE 恢复（G05）**：`motte-gap` 命名事件 + `GET /runs/{id}/events/snapshot`
  持久查询 + 单轮 500 事件上限 + 非法 Last-Event-ID 400；SDK `stream_events`
  断线重连/seq 去重/缺口补齐/终态对账；Web `useRunEvents` 同语义（partial 标记）。
- **CLI local/server（G04）**：显式 `--mode`/`MOTTE_CLI_MODE`；server 模式只走
  SDK HTTP，远端失败非零退出、绝不静默回退本地；新增 run 生命周期子命令；
  local/server 同输入同错误码（parity 测试固化）。
- **pytest/Trace/导出（G06-G07）**：opt-in `--motte-gate-report` 门禁读取插件
  （普通 pytest 零模型/零 Judge/零 Run）；`@motte_trace` 单次执行保证 + 脱敏 +
  64KiB 截断 + flush 失败不重跑；exporter 保持 `gate-exporter@1`（新增
  `write_gate_export` 文件输出，映射不变）。
- **打包（G08-G09）**：13 个 workspace 包真实 build-system/依赖/extras；
  `make wheels`；checkout 外干净 venv 安装测试（8 项）；CI 新增 packaging job；
  Dockerfile HEALTHCHECK + uv pin 对齐。
- **历史迁移（G10-G14）**：ImportManifest/SourceIdentity/MappingRecord/ImportReport
  契约；来源包安全加载（穿越/symlink/压缩配额/许可证/秘密字段拒绝）；dry-run 零
  目标修改；checkpoint apply/resume；同键冲突不覆盖；受限 rollback（共享工件
  保护）；imported Run 永不入队执行；缺字段保持 unknown、legacy summary 不伪造
  逐题分。
- **一致备份/恢复（G15-G16）**：维护屏障（API 503 + Worker 拒领）；引用制工件
  快照 + sha256 校验 + Manifest v2；staging 恢复全链校验 + 恢复守卫（Worker
  拒绝领取直至显式解除）+ 未决 Run 原样保留。
- **安全/保留（G17-G19）**：Host/Origin/Bearer token 中间件（默认 loopback，
  远程显式配置）；CORS 白名单；GC 默认 dry-run + pin/引用保护 + tombstone 审计；
  供应链：pip-audit/pnpm audit/trivy config（既有）+ license 清单检查。
- **发布（G20-G23）**：支持矩阵（docs/release/support-matrix.md）、升级/回退
  手册、切换手册（授权门控）、旧平台能力映射、cutover readiness 门禁测试。

## 已知边界（如实）

- 本机无 Docker/PG：真实 Compose build/up、PG 并发/迁移、pg_dump 备份为
  blocked/not_run（CI Linux + 真实 PG service 覆盖部分，见支持矩阵）。
- Windows 环境族（O_DIRECTORY 目录句柄等，M5 起登记）影响 Harbor trusted-root
  与部分子进程存活判定测试；CI Linux 不受影响。
- Judge 人工校准缺失 → 正式门禁对 experimental 证据 fail-closed（M5 起继承）。
- trace 事件无时间戳：GC v1 不做 DB 行裁剪（计划如实报告）。
- 阶段结论：`offline_verified`（本机+合成证据）；`stable_supported`/
  `cutover_ready` 需支持矩阵与三类替代场景全部真实证据（含真实旧导出、生产
  备份恢复、切换授权），当前为 `external_pending`。

## RC 冻结信息

| 项 | 值 |
|---|---|
| RC commit | <最终门禁后回填> |
| uv.lock | <回填 sha256 前 16> |
| wheels | `make wheels` → dist/（6 包，大小见构建日志） |
| Web dist | `pnpm --dir apps/web build` |
| Docker image | motteavl:local（本机未构建，CI/发布环境回填） |
| Alembic head | 0015_m7_platform_tables |
| OpenAPI | api/openapi.json（make openapi-check 无漂移） |
| exporter | gate-exporter@1（v1 内扩展，映射不变） |

## 升级

见 `docs/operations/upgrade.md`（M7 节）：0015 平台表、安全环境变量、维护模式、
恢复守卫、幂等键。回退见 `docs/operations/rollback.md`。
