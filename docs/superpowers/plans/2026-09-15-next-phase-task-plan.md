# 下一阶段任务安排：从基础骨架到可复核的真实执行链

日期：2026-09-15。基线 commit：`1aab975`（60 tests passed）。
本计划把"阶段 0–6"路线落成可执行任务，所有现状核查均基于当日仓库实际代码，与 `docs/PROGRESS.md` 的记载有出入处以代码为准。

## 现状盘点（路线图假设 vs 实际代码）

| 路线图判断 | 实际情况 | 关键证据 |
|---|---|---|
| API 仍是轻量基础实现 | 属实。仅 6 个路由；replay 通过临时替换 `service.provider` 在请求内同步执行；SSE 是一次性 dump，无 seq 恢复 | `apps/api/app/main.py` |
| Worker 无完整 broker 调度 | 属实。`celery_app = object()` 占位；无 `__main__` 入口；队列是进程内 dict，重启即丢；无人重扫 queued Run | `apps/worker/motte_worker/{celery_app,tasks}.py` |
| SQLite 有基础能力，PG 只是 boundary | 属实。SQLite 是单表 KV（`records(key, value)` 存 JSON blob），Run/TraceEvent/Score 无独立表；PG repository 恒抛 `UnsupportedStorageError` | `packages/storage/motte_storage/{repositories,postgres}.py` |
| Provider 有离线能力，未执行真实调用 | 属实。`HTTPTransport`（urllib，timeout/429/Retry-After）真实且已测；但 openai_compatible 只是 openai_chat 的别名 shim；无 canonical 落盘、无 usage/latency/retry_count 记录、无 live-smoke 命令 | `packages/provider-runtime/motte_provider/{transport,openai_compatible}.py` |
| Sandbox/Pi/Claude/Codex 以 dry-run、stub 为主 | 属实。`SandboxRunner` 非 dry-run 即 raise，`DockerSandbox` 是别名；`bridges/pi` 只有 package.json；CLI 只有 `doctor` 一个命令 | `packages/sandbox/`、`bridges/pi/`、`packages/cli/motte_cli/main.py` |
| Web 测试是 workspace 健康检查级别 | 属实。`apps/web/tests/` 为空目录，test 脚本为 `node -e` echo；仅一个 `getRun` API 函数；依赖全部声明为 `latest` | `apps/web/package.json` |
| pnpm-lock.yaml 未纳入版本控制 | 属实。文件在磁盘上（29KB），未被 .gitignore 忽略，只差 `git add` | `git status` |

其他与任务相关的基线事实：

- `RunService`（`packages/sdk-python/motte_sdk/service.py`）已被 API/CLI/Worker 共同 import，但构造路径各自为政：CLI 用模块级 `InMemoryRepository` 单例，Worker 默认同样，API 用 `MOTTE_DB_PATH`。
- 状态机现有 `queued/running/completed/failed/cancelled/unsupported`；`preparing/collecting/scoring/profile_stale` 不存在；`retry()` 已实现"创建 parent_run_id 子 Run"但无 API 入口。
- `rescore` 端点只置 `rescored: true` 标志，不调用 evaluator。
- 事件带持久化 seq（`event:{run_id}:{seq:08d}`），重启可续，这是 SSE 恢复的现成基础。
- CI 只跑 1 个 workspace 健康测试 + ruff + compileall + compose config；无 pnpm/Node 步骤。
- 版本基线：CI Python 3.12 / 本地 uv venv 3.12.11（无 `.python-version`，系统 python3 为 3.14）、pnpm 9.15.0（已由 packageManager 钉住）、uv 0.11.6、Node 24.18.0（未钉）。
- mypy strict 已配置但从未运行（当前代码大概率不过，列入阶段 6 而非阶段 0）。

## 全局顺序与依赖

```
阶段 0 基线 ─→ 阶段 1 执行内核 ─→ 阶段 2 真实 Provider ─→ 阶段 4 Sandbox/Agent/Harness ─→ 阶段 5 产品层 ─→ 阶段 6 质量门禁
                     │                    │
                     └──→ 阶段 3 PostgreSQL（可与阶段 2 并行，依赖阶段 1 定型实体表结构）
```

- 阶段 3 的表结构是阶段 1 commit 2 的 SQLite 实体表的移植，二者不要各自发明 schema。
- 阶段 6 中"replay-only CI"在阶段 0 即以全量 pytest 形态建立，阶段 6 只是补齐其余门禁。
- 阶段 4 的 BuiltinReAct 依赖阶段 2 的 provider 真实调用能力。

跨阶段不变量（每个任务、每个 commit 都必须保持）：

1. 默认测试零网络、零花费：只用 fixture 与 replay；`live` 标记测试默认不运行。
2. 凭据只存在于进程环境，不进日志、Trace、SSE、CLI 输出、Artifact、数据库。
3. 每个任务附带 focused tests，一个任务一个 commit（沿用 PROGRESS.md 约定）。
4. Web UI 保持简体中文。
5. Windows 一律通过 Docker/WSL2（见运行安全 ADR）。

---

## 阶段 0：可复现开发基线

**目标**：任何开发者 clone 后按 README 一步跑通全部门禁；CI 与本地命令完全一致。

### 任务分解

- **P0-1 提交 pnpm-lock.yaml 并固定 Web 依赖**
  - `git add pnpm-lock.yaml` 并提交。`apps/web` 依赖全部声明为 `latest`，lockfile 是唯一版本事实，必须入库。
  - 顺手把 `apps/web/package.json` 中依赖从 `latest` 改写为 lockfile 中解析出的具体版本范围，避免 `pnpm update` 静默跳大版本。
- **P0-2 工具链版本钉住**
  - 新增 `.python-version`（`3.12`）与 `.nvmrc`/`.node-version`（`24`），消除"系统 3.14 vs CI 3.12"漂移。
  - `pyproject.toml` 增加 `[tool.uv] required-version`（或提交 `.tool-versions`），钉住 uv。
  - pnpm 已由 `packageManager: pnpm@9.15.0` 钉住，不需要额外动作。
- **P0-3 Makefile 统一命令**
  - 新增根 `Makefile`：`make test`（`uv run pytest -q`）、`make lint`（`uv run ruff check .` + `uv run python -m compileall -q packages apps`）、`make dev`（API + Web 同起：`uv run uvicorn apps.api.app.main:app --reload` + `pnpm --dir apps/web dev`）、`make replay`（`uv run pytest -m replay -q`）、`make web-build`（`pnpm install --frozen-lockfile && pnpm --dir apps/web build`）。
  - `scripts/*.ps1` 占位脚本删除或并入 Makefile（Windows 走 ADR 的 Docker 路径）。
- **P0-4 README 与运维文档**
  - README 写清三个进程的启动方式：API（uvicorn 命令如上，`apps/api/app/main.py:63` 已有模块级 `app`）、Worker（阶段 1 前如实注明"无独立入口，经 compose/测试驱动"）、Web（`pnpm --dir apps/web dev`）。
  - 补 `.env.example` 各变量当前是否真正被消费的说明（`REDIS_URL`、`DATABASE_URL`、`OTEL_*` 目前均无代码消费，标注"预留"）。
  - 扩充 `docs/operations/install.md`（当前 3 行）。
- **P0-5 CI 对齐本地命令**
  - `.github/workflows/ci.yml`：全量 `uv run pytest -q`（替换只跑 `tests/test_workspace_health.py` 的一行）；新增 Node 侧 job——pnpm/action-setup + setup-node（带 cache）→ `pnpm install --frozen-lockfile` → `pnpm --dir apps/web build` → `pnpm -r test`；保留 ruff、compileall、`docker compose config`。
  - pytest 默认排除 live（`-m "not live"`，当前无 live 测试，属防御性配置）。
- **P0-6 生成物防回归**
  - `.gitignore` 已覆盖 `.pytest_cache/.ruff_cache/var/*.db/var/artifacts/*`，无需改动；验证干净 clone + 全部门禁通过即可，不删除本地 `var/runs.db`。

### 验收门

在干净 clone 上依次通过：`uv sync`、`pnpm install --frozen-lockfile`、`uv run pytest -q`、`uv run ruff check .`、`pnpm --dir apps/web build`、`docker compose -f infra/docker-compose.yml config`；CI 全绿且步骤与上述一一对应。

### 建议提交

- `chore: make local setup reproducible`

---

## 阶段 1：Run 执行内核（下一阶段核心）

**目标状态机**：`create → queued → preparing → running → collecting → scoring → completed`，终态含 `failed/cancelled/unsupported`，另有 `profile_stale` 与 retry 子 Run 语义。

现状差距：状态机缺 4 个状态；存储是 KV blob 无实体表；CaseRun 从不持久化；Worker 无入口、无恢复；replay 靠 mutate 共享 service；rescore 是空操作；SSE 无恢复。

### 任务分解（对应 5 个 commit）

- **P1-1 `feat: add shared run service`**
  - 在 `motte_sdk` 增加 `build_run_service()` 工厂：统一读取 `MOTTE_DB_PATH`（默认 `var/runs.db`）构造 repository + `RunService`；`apps/api/app/main.py`、`apps/worker/motte_worker/tasks.py`、`packages/cli/motte_cli/commands.py` 全部改用工厂，删除 CLI/Worker 各自的 `InMemoryRepository` 默认单例。
  - 扩展状态机：新增 `preparing/collecting/scoring/profile_stale`，集中定义合法迁移表并校验非法迁移。
  - cancel：记录 `reason`，幂等，阻断后续 case 执行（非仅改状态）。
  - retry：保留 `parent_run_id` 子 Run 语义，新增 `POST /api/v1/runs/{id}/retry`。
  - rescore：改为从持久化 Trace 重算 Score 的真实实现，删除只置 flag 的版本。
- **P1-2 `feat: add durable run and case repositories`**
  - `motte_storage` 定义实体 repository 接口并双实现（SQLite + InMemory）：`RunRepository`、`CaseRunRepository`、`TraceEventRepository`、`ScoreRepository`。SQLite 建结构化表 `runs/case_runs/trace_events/scores`，替代这四类实体的 KV blob（KV 表可保留给其他用途）。
  - 约束：`trace_events UNIQUE(run_id, seq)`；`case_runs UNIQUE(run_id, case_id)`（幂等执行的基础）。
  - `RunService` 改为依赖实体 repository 接口；本地旧 `var/runs.db` 直接重置，在 upgrade 文档注明。
- **P1-3 `feat: add queued worker execution`**
  - Worker 入口：`apps/worker/motte_worker/__main__.py`（`python -m motte_worker`），轮询 `status='queued'` 的 Run 并执行；用原子 `UPDATE ... WHERE status='queued'` 抢占避免双执行；重启后重扫 queued Run（验收：kill -9 后重启继续处理）。
  - Celery 真实化：替换 `celery_app = object()`；broker 用 `REDIS_URL`；`MOTTE_WORKER_MODE=celery|loop`（本地无 Redis 时 loop 模式即可全流程可用）；compose 的 worker 服务补 `command`。
- **P1-4 `feat: add replay api end to end flow`**
  - 去掉 `POST /runs/{id}/replay` 里 mutate `service.provider` 的做法：provider 选择改为 `RunService.execute()` 参数或 Run manifest 字段。
  - SSE 升级：`GET /runs/{id}/events` 支持 `?after=<seq>` / `Last-Event-ID` 断线恢复，轮询新事件直到 Run 进入终态，附心跳注释行；事件源仍是持久化 repository。
  - replay 一致性 golden 测试：同一 fixture 经 API、CLI、SDK 三个入口执行，TraceEvent（类型与 seq）与 Score 完全一致。
- **P1-5 `test: cover run cancellation retry and rescore`**
  - 按验收门补齐：同 Run 重复执行不产生重复 CaseRun（依赖 P1-2 唯一约束）；Worker 重启续跑 queued；cancel 阻断执行且持久化 reason；retry 生成 `parent_run_id` 子 Run 且原 Run 不被污染；rescore 用调用计数 provider 断言模型调用次数为 0。

### 验收门

同一 Run 重复执行不产生重复 CaseRun；Worker 重启后继续处理 queued Run；cancel 阻止后续执行并记录原因；retry 创建新子 Run；rescore 不重新调用模型；replay 的 Trace 和 Score 在 API、CLI、SDK 三入口一致。

---

## 阶段 2：第一个真实 Provider（openai_compatible）

**目标**：让一次 Run 可以真实调用任何 OpenAI 兼容端点（本地 vLLM/OLLama 兼容层或云端），且全部证据可复核、密钥零泄漏。

### 任务分解

- **P2-1 真适配器**：`openai_compatible.py` 从别名 shim 改为真实 adapter，基于 `HTTPTransport` 组装 `/chat/completions` 请求，复用 `openai_chat.normalize_response`。
- **P2-2 传输健壮性与计量**：timeout、取消传播、网络错误分类、有限重试（429/Retry-After 已有）；记录 `usage`、`latency_ms`、`retry_count`、错误分类（network/rate_limit/auth/protocol/unsupported）到 TraceEvent。
- **P2-3 canonical 与脱敏**：持久化 canonical request/response（TraceEvent + Artifact）；`motte_trace.redaction` 接入 transport 落盘路径；新增测试断言 API key 不出现在日志、Trace、SSE、CLI 输出、Artifact。
- **P2-4 价格与 manifest**：`PriceTable` 版本化（文件加载）；`ModelProfile/ParameterProfile/PriceTable` 写入 Run manifest；成本按请求发生时的 `price_table_version` 计算；未知价格存 `null`（显示 unknown），不猜。
- **P2-5 strict 预检**：`capabilities.validate_parameters` 在任何付费调用之前执行，unsupported 直接落 `unsupported` 状态并返回明确错误。
- **P2-6 CLI 重构与 live smoke**：`motte_cli` 从单命令 doctor 改为子命令结构（`run/replay/provider/model/live-smoke`）；实现
  `uv run python -m motte_cli live-smoke --provider openai-compatible --model <model-id>`；
  结果按模板写入 `docs/operations/live-smoke-log.md`（同时关掉 PROGRESS.md 的 live smoke 待办）；CI 显式排除 `-m live`。

### 验收门

默认测试全部 fixture/replay；live smoke 只由操作者显式启动；API key 不出现在日志、Trace、SSE、CLI、Artifact；strict 参数不支持时在付费调用前失败；成本计算使用请求发生时的 price table version。

### 后续接入顺序

1. OpenAI Responses（真适配器，替换 shim）
2. Anthropic Messages（真适配器，替换 shim）
3. 其他厂商 profile

---

## 阶段 3：PostgreSQL 生产存储

**目标**：SQLite foundation 具备生产替换路径；schema 与阶段 1 的实体表同构，不另起炉灶。

### 任务分解

- **P3-1 迁移管理**：引入 alembic；实装 `migrations/env.py`（当前为占位注释）；`migrations/versions/0001_initial.py`（当前只有 revision 元数据无 DDL）写入完整建表：`ProviderConnection/ModelProfile/PriceTable/DatasetVersion/ScenarioVersion/Run/CaseRun/TraceEvent/Artifact/Score`。
- **P3-2 PG repository**：用 psycopg 实装全部实体 repository，替换 `PostgresRepository` 恒抛 `UnsupportedStorageError` 的行为；实装 `db.create_engine()`。
- **P3-3 存储选择解耦**：`MOTTE_STORAGE=sqlite|postgres` + DSN 配置；API/Worker 只 import repository 接口（`apps/api/app/dependencies.py` 里从未被使用的 `get_run_service` 正好在此启用）。
- **P3-4 约束**：`(run_id, seq)` 唯一；版本资源（ModelProfile/PriceTable/Dataset/Scenario）按 (name, version) 唯一；CaseRun 去重约束与 SQLite 相同。
- **P3-5 集成测试**：CI 增加 postgres service container；测试链路：空库 → migration → API create → worker execute → 查询 Run/Trace/Score。
- **P3-6 Compose**：postgres 服务加 migration 执行与 healthcheck，API/Worker `depends_on: condition: service_healthy`；补 `docker compose config` 覆盖。

### 验收门

空数据库 → migration → API create → worker execute → 查询 Run/Trace/Score，全链路在 PG 上通过；SQLite 路径行为不变。

---

## 阶段 4：真实 Sandbox、Agent 与 Harness

**接入顺序**：Docker Sandbox → BuiltinReAct → Pi bridge → Claude CLI → Codex app-server。

现状全部为 stub：`SandboxRunner.run` dry-run 或 raise、`DockerSandbox` 是别名、`BuiltinReActRuntime` 无循环、`bridges/pi` 无代码、Claude/Codex 只实例化 dry-run 的 `ProcessRunner`。

### 统一适配器契约（每个都必须满足）

`prepare/start/send/events/interrupt/collect/cleanup` + 版本 probe + parser version + 受控工作目录 + 超时与取消 + artifact 收集 + 统一 TraceEvent + 失败/unsupported 状态映射（对接阶段 1 状态机）。

### 任务分解

- **P4-1 Docker Sandbox**（用 docker SDK 替换别名）：
  - 默认 `--network none`；command policy 与 network policy 分离（沿用并强化 `policy.py`）；拒绝 privileged、Docker socket 挂载、workspace 路径穿越、未声明环境变量。
  - 资源限制全部生效：CPU、内存、PID、磁盘、输出体积、TTL。
  - cleanup 在成功/失败/取消三条路径都执行（`finally` + 测试覆盖三种路径）。
  - artifact 收集从容器 workspace 拷出并过 `ArtifactStore`。
- **P4-2 BuiltinReAct**：真实 agent 循环（LLM 调用走阶段 2 provider），工具调用经 `ToolRegistry` 权限模式，预算与步数上限，全程 TraceEvent。
- **P4-3 Pi bridge**：`bridges/pi` 从空 package.json 落地为真实桥接进程（JSONL 协议编解码已在 `motte_agent/protocol.py`，桥接层补进程管理）。
- **P4-4 Claude CLI harness**：`ProcessRunner` 实装子进程生命周期（spawn/wait/kill/超时）、`claude --version` probe、JSONL parser 带 parser version、terminal channel 保持 deny-by-default。
- **P4-5 Codex app-server harness**：同上，面向 app-server 通道。

### 验收门

Sandbox 五条安全断言各有独立测试（无网络/策略分离/禁止项/资源限制/恒 cleanup）；每个适配器有 probe 与版本记录；取消与超时都会触发 cleanup 并落终态。

---

## 阶段 5：Evaluator 与 Web 产品层

**前提**：阶段 1–4 执行链稳定后再动产品层，避免为不稳定内核堆 UI。

### 任务分解

- **P5-1 API 资源化**：providers、models、datasets、scenarios、agents、skills、harnesses、runs、reports 九组资源的 CRUD/list 端点；runs 组补齐阶段 1 之后的 cancel/retry/rescore/replay/events 全套。
- **P5-2 Web 功能页**（现状：静态单页 + 一个 `getRun` 函数）：Provider/Model 管理、Scenario 编辑、Run 控制（创建/cancel/retry）、SSE 时间线（`EventSource` + Last-Event-ID 恢复）、Trace 过滤、Artifact 查看、Score 与 Compare、Report 导出。UI 简体中文，任何页面不显示凭据。
- **P5-3 TypeScript 类型生成**：从 FastAPI OpenAPI 导出 schema，`openapi-typescript` 生成类型入库，`src/api/client.ts` 改用生成类型。
- **P5-4 测试升级**：vitest 真实组件测试（当前 `apps/web/tests/` 为空、test 脚本是 echo）替换健康检查；`tests/api` 增加资源端点 integration test。

### 验收门

Web 能走完"建 Provider → 建 Run → 看时间线 → 看 Trace/Score → 导出 Report"；类型由 OpenAPI 生成且 CI 校验无漂移；组件测试覆盖核心交互。

---

## 阶段 6：发布与质量门禁

| 门禁 | 说明 | 依赖 |
|---|---|---|
| replay-only CI | 全量 pytest 默认 `-m "not live"`（阶段 0 已建，此处固化文档化） | 0 |
| PostgreSQL migration test | CI 起 PG，跑空库→migration→全链路（P3-5 固化） | 3 |
| OpenAPI diff | 提交 `openapi.json`，CI 用 oasdiff 对比，防止破坏性 API 变更 | 5 |
| TS type generation check | 类型重新生成后 diff 必须干净 | 5 |
| 依赖与镜像扫描 | pip-audit、pnpm audit、compose 镜像 trivy 扫描 | 0 |
| backup/restore | `var/` 与数据库的备份恢复 runbook + 演练脚本 | 3 |
| artifact cleanup | TTL 清理任务与 dry-run 模式 | 3 |
| rollback | 版本回退流程文档（含 migration downgrade 策略） | 3 |
| compatibility matrix | Provider/CLI/bridge 版本兼容矩阵，替换 3 行占位的 `protocols/provider-compatibility.md` | 2/4 |
| live smoke 记录模板 | `docs/operations/live-smoke-log.md` 模板化（P2-6 已建，此处统一格式） | 2 |
| mypy strict | 当前配置了 strict 但从未运行；在代码量稳定后分期修复并纳入 lint | 5 |

---

## 立即可执行的第一步（阶段 0，预计半天内完成）

1. `git add pnpm-lock.yaml`，同时把 `apps/web/package.json` 的 `latest` 依赖改为具体版本，提交。
2. 新增 `.python-version`（3.12）、`.node-version`（24）、`[tool.uv] required-version`。
3. 写 `Makefile`（test/lint/dev/replay/web-build），删或并 `scripts/*.ps1`。
4. README 补三进程启动说明 + `.env.example` 变量消费状态标注。
5. 改 CI：全量 pytest + pnpm frozen install + web build job。
6. 干净 clone 跑一遍六条验收命令，通过后提交 `chore: make local setup reproducible`，随后进入阶段 1 P1-1。
