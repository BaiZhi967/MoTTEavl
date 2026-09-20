# Terminal-Bench（Harbor）操作指南

> 状态：**固定 Harbor 0.23.0 + 仓库自有确定性任务 + 真实 Docker（oracle Agent）
> 已在本地验证；真实模型/Agent live 层与上游完整任务集（89/10 题）未执行
> （blocked / not_run）**。
> 本文只把本机实际跑过的组合写成"已验证"；逐条登记见
> [兼容矩阵](harbor-compatibility.md) 与 [M3 验证记录](../verification/M3.md)。

## 1. 职责划分

一个 terminal-bench Run 只启动一个外部 Job，职责两侧分界固定：

| 侧 | 负责 | 不负责 |
|---|---|---|
| Harbor（固定版本 Runner） | 任务环境生命周期：容器创建/构建、任务在容器内执行、Verifier 执行、原生产物（trial 目录、`result.json`、reward 文件、日志） | 平台 Run/Job 状态、证据冻结、评分、报告 |
| MoTTEavl | Job 生命周期（prepare → start → poll → collect → cleanup）、TrialPlan 冻结、证据内容寻址冻结、Task/Trial 身份、评分与报告、取消与残留核查 | 复制 Harbor 的环境管理内核；在任务容器内暴露 Docker socket、宿主凭据目录或平台数据库 |

平台**不把整个任务目录当 dataset 交给 Harbor**：冻结配置使用显式任务列表
（`job.tasks`，`datasets: []`），避免上游按 basename 合并同名任务（见
[任务与 Trial 身份协议](../protocols/task-trial-identity.md)）。

## 2. 固定版本与数据来源

本次会话核对（2026-09-20）：

- `harbor==0.23.0`（PyPI，Apache-2.0，requires-python >=3.12）。
- Terminal-Bench 2.x 任务集来源取自 Harbor 官方 registry：
  `https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json`。

  | dataset | version | 任务数 | 仓库 | commit |
  |---|---|---|---|---|
  | `terminal-bench` | `2.0` | 89 | `https://github.com/laude-institute/terminal-bench-2.git` | `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c` |
  | `terminal-bench-sample` | `2.0` | 10 | `laude-institute/terminal-bench-2-0-sample.git`（`https://github.com/laude-institute/terminal-bench-sample`） | `7e917f35c281188532772312d4ad91ca9274febc` |

  sample 集任务路径为 `sample/<name>`。
- 固定来源（pinned-source）tarball 地址：
  `https://codeload.github.com/<owner>/<repo>/tar.gz/<commit>`。
- **观测值**（2026-09-20）：sample 集上述 commit 的 tarball sha256 为
  `ad0f9f5fde5efd7c2e2b63ae4c64d867783a4e5557e0d7b4a5876234694e2b13`，
  12,570,253 字节、110 个条目。该值只对本次下载有效；**重新下载后必须重新
  计算 hash**，不得沿用此值，也不得凭它宣称内容未变。

## 3. 安装与部署布局

```bash
scripts/runner/install-harbor /opt/motte-runner
```

安装脚本：创建固定的 Python 3.12 venv（已存在可执行 `bin/python` 则复用），
断言解释器为 3.12，`uv pip sync` 安装
`scripts/runner/harbor-0.23.0-py312.lock` 的完整依赖集合，把桥接模块
（`motte_benchmark.harbor` 的 `__init__`/`entry`/`tasks`/`config`）拷入该
环境的 `purelib`，安装 `scripts/runner/harbor-entry`，最后用真实 Harbor
模型（`JobConfig`/`TrialConfig`/`TrialResult`/`VerifierResult`）做导入自检。

部署布局：

| 位置 | 内容 |
|---|---|
| `/opt/motte-runner` | Runner 根目录（独立 venv，不向 API 环境安装 Harbor） |
| `/opt/motte-runner/bin/harbor-entry` | 平台 wrapper（受控入口） |
| `$MOTTE_WORK_DIR/harbor/config.json`、`harbor/plan.json` | 平台冻结的原生配置与 TrialPlan |
| `$MOTTE_WORK_DIR/harbor/job-location.json` | Runner 写回的 Harbor Job 目录位置 |
| `$MOTTE_WORK_DIR/.motte-job-complete` | 原子完成标记（含 exit_code） |

环境覆盖：`MOTTE_RUNNER_ROOT`（Runner 根，缺省取 wrapper 相邻上一级或
`/opt/motte-runner`）、`MOTTE_RUNNER_PYTHON`（固定解释器）、
`MOTTE_TASK_ROOT`（受控任务数据根，Harbor 从这里解析任务）。

adapter 注册（与 M2 同一受控加载）：`MOTTE_RUNNER_CONFIG` 指向的 JSON 或
缺省 `var/runner/adapters.json` 存在时按条目注册：

```json
{"adapters": [{"benchmark": "terminal-bench", "argv": ["/opt/motte-runner/bin/harbor-entry"],
               "data_root": "/srv/motte/terminal-bench-tasks"}]}
```

两者皆无但 `/opt/motte-runner/bin/harbor-entry` 实际存在时，按固定 wrapper
自动注册；都不满足则 `RUNNER_NOT_CONNECTED`，创建入口在提交前拒绝。

## 4. 工作流

```
prepare   只读准备受控任务根目录：遍历、读字节、算内容 hash、写不可变 revision
          （不执行任务包脚本、不 docker build、不调用模型）
preflight 只读预检：fail-closed，零模型调用、零任务启动；不通过即拒绝创建
run       创建 queued Run（202）；由 Worker 经 Dispatcher/外部 Job 启动 Harbor
status    Task 层汇总（计划/有效/通过 Trial、覆盖率、覆盖门禁）
drill-down GET /runs/{id}/tasks → /tasks/{key}/trials → /trials/{trial_id}
```

冻结顺序：TrialPlan 与 Harbor 原生配置在创建时写入 manifest；adapter
`prepare` 把配置/计划落进工作目录（零执行）；`start` 是唯一产生执行副作用的
操作；采集按"读受控字节 → 冻结内容寻址 Artifact → 从同一份冻结内容解析"。

## 5. CLI

与 API 共用 `motte_sdk.terminalbench` 门面，任务身份、预检原因码与 Trial 视图
两端一致；执行只入队，CLI 进程不跑任务。

```bash
# 只读准备（固定来源加 --pinned-source）
uv run python -m motte_cli terminal-bench prepare \
    --task-root DIR --source-id ID --revision REV \
    [--license-id L --license-evidence E --pinned-source]

# 任务清单
uv run python -m motte_cli terminal-bench tasks [--dataset-revision REV --json]

# 只读预检（零模型调用/零任务启动）
uv run python -m motte_cli terminal-bench preflight \
    [--agent-id oracle --agent-version 1.0.0 --n-trials N --task-keys a,b --dataset-revision REV]

# 创建 queued Run（需已发布 ModelProfile；预检不通过即拒绝）
uv run python -m motte_cli terminal-bench run --model MODEL_ID \
    [--n-trials N --aggregation first-trial|mean-success --task-keys a,b \
     --agent-timeout-sec S --verifier-timeout-sec S --job-timeout-sec S]

# Task 层结果 / 下钻
uv run python -m motte_cli terminal-bench status --run-id RUN [--json]
uv run python -m motte_cli terminal-bench trials --run-id RUN --task-key KEY
uv run python -m motte_cli terminal-bench trial --run-id RUN --trial-id ID
```

各子命令支持 `--db`（SQLite 路径，缺省 `MOTTE_DB_PATH`）；`--task-keys` 为
逗号分隔的 `task_key` 子集。`run` 输出的提示命令为：

```bash
uv run python -m apps.worker.motte_worker --once
```

## 6. HTTP 端点

| 方法与路径 | 说明 |
|---|---|
| `GET /api/v1/benchmarks/terminal-bench` | 概览：已准备 revision、任务数、Runner 连接状态、本套件 Run 摘要 |
| `POST /api/v1/benchmarks/terminal-bench/prepare` | 只读准备（201；治理/结构失败 422 + 原因码） |
| `GET /api/v1/benchmarks/terminal-bench/tasks` | 任务清单（`dataset_revision` 可选） |
| `GET /api/v1/benchmarks/terminal-bench/preflight` | 只读预检（`model`/`n_trials`/`task_keys`/`agent_id`/`agent_version`/`dataset_revision`） |
| `POST /api/v1/benchmarks/terminal-bench/runs` | 创建 Run（202）；未准备/模型非 published/Runner 未连接/预检失败 → 422 且 0 次启动 |
| `GET /api/v1/runs/{run_id}/tasks` | Task 层汇总（计划/有效/通过、覆盖、门禁） |
| `GET /api/v1/runs/{run_id}/tasks/{task_key}/trials` | 某 Task 的全部计划 Trial（含 pending） |
| `GET /api/v1/runs/{run_id}/trials/{trial_id}` | 单 Trial 详情（终止、Verifier、证据引用与完整度；严格校验 run 归属，否则 404） |

Web：`/terminal-bench` 五页（操作/任务/监控/结果/比较），数据全部来自上述端点。

## 7. 预检与原因码

API/CLI 进程**不执行 docker**：Runner 侧探测结果写入 JSON，路径由
`MOTTE_HARBOR_PREFLIGHT_REPORT` 指定，字段为 `available`、
`server_version`、`platform`、`disk_free_bytes`、`disk_required_bytes`、
`images`。没有该报告（或不可读）时按不可用处理，预检 fail-closed 返回
`DOCKER_UNAVAILABLE`。

任何原因码都表示"不能开始"，此时 `model_calls` 与 `task_starts` 均为 0。
完整原因码目录（实现中的全部取值）：

| 原因码 | 含义 |
|---|---|
| `ENVIRONMENT_TYPE_UNSUPPORTED` | 所选环境类型未在本平台验证（当前仅 `docker`） |
| `AGENT_NOT_PINNED` | 未指定 Agent，或 Agent 未固定到具体 ID |
| `AGENT_VERSION_NOT_PINNED` | Agent 未固定到具体版本 |
| `DOCKER_UNAVAILABLE` | Runner 上无法访问 Docker daemon |
| `DOCKER_PLATFORM_UNSUPPORTED` | 宿主平台不受支持（需 linux/amd64 或 linux/arm64） |
| `DOCKER_DISK_LOW` | 磁盘余量低于任务所需 |
| `DOCKER_IMAGE_MISSING` | 所需镜像在 Runner 上不存在 |
| `AGENT_DEPENDENCY_MISSING` | Agent 依赖在 Runner 环境中缺失 |
| `RUNNER_ENV_CREDENTIALS_INLINE` | Runner 环境里出现明文凭据，必须改为引用 |
| `TASK_CONTAINER_HOST_ENV_EXPOSED` | 任务容器会继承宿主凭据环境变量 |
| `TASK_HOST_PATH_EXPOSED` | 任务容器挂载了宿主敏感路径 |
| `TASK_DOCKER_SOCKET_EXPOSED` | 任务容器挂载了 Docker socket，拒绝执行 |
| `NETWORK_POLICY_UNVERIFIED` | 无法验证网络策略，按 fail-closed 拒绝 |
| `NETWORK_POLICY_INVALID` | 网络策略取值非法 |
| `VERIFIER_VISIBILITY_UNVERIFIED` | 无法确认 Verifier/gold 可见性边界 |
| `TASK_WITHOUT_VERIFIER` | 所选任务没有 Verifier（`tests/`），无法产生评分证据 |
| `NO_TASKS_SELECTED` | 没有选择任何任务 |

## 8. 安全边界

- **任务容器永远不获得**：Docker socket、宿主凭据目录（`~/.ssh`、`~/.aws`、
  `~/.config/gcloud`、`~/.docker`、`~/.kube` 等）、平台数据库文件与平台内部
  环境变量；命中即预检拒绝（上表对应原因码）。
- 凭据只以 `{"ref": "env:NAME"}` 引用形式进入 Runner 配置；任何原始值
  `SECRET_VALUE_IN_CREDENTIALS` 拒绝，配置 dump/日志/Artifact 不落明文。
- 网络策略与 Verifier 可见性**记入 profile fingerprint**（`profile_fingerprint`）。
  任一项与上游不同（挂载、网络策略非 `allowed`、Verifier 可见性非
  `upstream`）时 Run 标记 `platform_custom_profile: true`，**不得对外宣称为
  官方同口径成绩**。
- 权限边界如实记录：模型身份与用量由 Runner 观测（`observation_boundary`），
  平台只能核验 Runner 退出、reward 文件与 trial 结果；不可观测项标
  `unknown`，不补填。

## 9. 成本与限额

- 未观测到的成本保持 `null`（**不填 0**）；未知成本的 Trial 单独计数。
- `per_success_usd` 在没有成功 Trial 时返回不适用（`null`），不扩大分母、
  不自动补跑；已知成本小计包含失败 Trial 的费用（`includes_failed_trials_in_numerator`）。
- Agent、Verifier、环境构建/启动与总时长分别报告，不把容器启动时间当作
  模型推理延迟。
- 平台侧限额：计划重复 `n_trials` ≤ 32；原生 Runner 重试本阶段不开放
  （`HARBOR_RETRY_NOT_ALLOWED`），计划重复用 `n_trials` 表达；三层期限
  （`agent_sec` / `verifier_sec` / `job_sec`）独立设置。

## 10. 本阶段不包含

- 真实模型/Agent 的 live 验收（**blocked**：需用户显式授权可审阅的任务清单、
  模型、Profile、重复数、预算与期限）。
- 上游完整任务集实机运行：89 题 `terminal-bench 2.0` 与 10 题
  `terminal-bench-sample 2.0`（**not_run**）。
- 云端 Harbor 服务、多机调度、训练数据生成、自动公开提交榜单。
- 除 `docker` 之外的环境类型；除 `oracle 1.0.0` 之外未经验证的 Agent。

分层验收清单见 [兼容矩阵](harbor-compatibility.md)；本机真实命令与结果见
[M3 验证记录](../verification/M3.md)。
