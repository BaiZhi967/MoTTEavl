# Harbor / Terminal-Bench 兼容矩阵

> 状态：**只登记本阶段实际验证过的组合**。矩阵为空缺即"未验证"，不是"应当可用"。
> 逐条命令与结果见 [M3 验证记录](../verification/M3.md)；操作步骤见
> [Terminal-Bench 操作指南](terminal-bench.md)。

## 1. 登记口径

"Harbor 支持 X" **不等于**"本平台验证了 X"。本矩阵按
**dataset-version × agent-profile × environment** 逐组合登记：

- 只有本机实际跑过、且证据可指向原始产物的组合才标 `已验证`；
- 未跑过的组合一律 `not_run`；需要用户授权/外部条件的组合标 `blocked`；
- 一个组合通过不推导相邻组合（换 Agent、换镜像、换网络策略、换宿主架构都算新组合）；
- 平台侧 allowlist 是**有类型的 Agent 表**（`AGENT_SPECS`）：`oracle`（固定
  1.0.0、零模型）+ `claude-code`（操作员显式钉住 CLI 版本、必须提供模型与凭据
  引用）。未登记的 Agent 在配置构造阶段拒绝（`HARBOR_AGENT_UNSUPPORTED`）——
  "未验证"是被强制的，不只是文档约定；
- **"实现已就绪"与"组合已验证"是两件事**：`claude-code` 的映射实现与**离线原生
  配置校验**（真实 Harbor 的 `JobConfig` + Agent options 模型；零容器、零模型
  调用）已完成，但本表不因此把它记为"已验证组合"；真实调用仍为 `blocked`；
- 安全/可见性与上游不同的组合必须带不同 `profile_fingerprint` 并标
  `platform_custom_profile: true`，不得对外宣称为官方同口径成绩。

验证层取值：`local_docker`（真实固定 Harbor + 真实 Docker + 确定性 Agent）、
`native_config_offline`（真实固定 Harbor 模型校验冻结配置，零容器/零模型调用）、
`offline_fixture`（脱敏真实产物 + 纯 Parser/存储，不含 Docker）、`not_run`。

## 2. 矩阵

| dataset-version | agent-profile | environment | verification layer | result | evidence |
|---|---|---|---|---|---|
| 仓库校准夹具集（`hello-pass` / `hello-fail`；错误路径夹具 `agent-timeout` / `verifier-timeout`，均非上游任务集） | `oracle` 1.0.0 | docker（本机 linux/arm64 引擎，Docker 27.4.0） | `local_docker` | **已验证**：4 Trial（2 Task × 2 attempt），2 通过 / 2 失败，`pass@k` 出现在 Harbor 自己的 job result；错误路径另一次 Job 得到 `AgentTimeoutError`（Verifier 仍给出 reward 0.0）与 `VerifierTimeoutError`（无 reward 文件）。全程无模型调用、无成本 | 脱敏原始产物 `tests/fixtures/benchmarks/harbor/samples/pass-fail-2x2`、`.../errors-timeout`（含 `SOURCES.json` 文件清单与逐文件 sha256）；`docs/verification/M3.md` |
| 仓库校准夹具集 | `oracle` 1.0.0 | docker（本机 arm64 引擎） | `local_docker` | **已验证**（review R02–R07 复验）：不可变任务副本复验、任务自带 Compose 拒绝、运行中取消（容器停止 + 诱饵保留）、SIGKILL 残留定位、真实 `job_sec` 期限中断；命令见 `docs/verification/M3.md` | `tests/integration/test_harbor_live_cancel_and_deadline.py`、`tests/integration/test_harbor_job_fixture.py`（`MOTTE_HARBOR_RUNNER_PYTHON` 门控，本次实跑通过） |
| 仓库校准夹具集 | `claude-code` 2.0.30（模型 `anthropic/claude-sonnet-4-5`，凭据 `env:ANTHROPIC_API_KEY`） | docker | `native_config_offline` | **原生配置已验证**：真实 Harbor 0.23.0 的 `JobConfig` 与 `ClaudeCodeOptions` 接受该冻结配置（`cli_version=2.0.30`、`model_name` 正确、凭据引用可解析）；**零容器、零模型调用**。真实调用仍 `blocked`（见下一行） | `tests/integration/test_harbor_agent_native.py`（实跑通过）；work dir 里 `harbor/config-validation.json` |
| 仓库校准夹具集 | `claude-code` 任意版本 | docker | `blocked` | 真实模型调用需用户显式授权：可审阅任务清单、数据集 revision、模型、Profile、重复数、预算、期限与命令；未授权前不执行 | — |
| 仓库校准夹具集 | `oracle` 1.0.0 | docker（linux/amd64 宿主） | `not_run` | 未执行；本机为 arm64 引擎 | — |
| `terminal-bench-sample` 2.0（10 题，`sample/<name>`） | `oracle` 1.0.0 | docker | `not_run` | 未执行完整 10 题集（仅准备/身份路径有测试） | `tests/benchmarks/test_harbor_task_identity.py`（夹具准备），完整集运行无证据 |
| `terminal-bench` 2.0（89 题） | `oracle` 1.0.0 | docker | `not_run` | 未执行完整 89 题集 | 同上；89 题清单来源见操作指南第 2 节 |
| 任一数据集 | 真实模型 Agent（非 oracle） | docker | `blocked` | 需用户显式授权：可审阅任务清单、模型、Profile、重复数、预算与期限；未授权前不执行 | — |
| 任一数据集 | `oracle` 1.0.0 | 受限网络策略（`none` / `restricted`） | `not_run` | 未执行；受限策略会产生 `platform_custom_profile: true` | 预检逻辑与指纹有测试：`tests/benchmarks/test_harbor_preflight.py` |
| 任一数据集 | `oracle` 1.0.0 | Verifier 可见性变体（非 `upstream`） | `not_run` | 未执行；变体必须标平台自定义 Profile | 同上 |
| 任一数据集（含任务自带 `environment/docker-compose.yaml`） | `oracle` 1.0.0 | docker | `offline_fixture` | **已验证**：恶意 Compose（Docker socket、`/:/host`、privileged、host network 等）在零任务启动、零模型调用时被具名原因码拒绝 | `tests/benchmarks/test_harbor_task_compose_policy.py` |

## 3. 未验证项的处理

- 未验证组合不得进入"支持"清单，也不得据其他组合的结果推断；
- 请求未验证 Agent / 环境类型 / 未知原生参数时，创建前即拒绝
  （`HARBOR_AGENT_UNSUPPORTED`、`HARBOR_ENVIRONMENT_UNSUPPORTED`、
  `HARBOR_CONFIG_UNKNOWN_FIELD`），不"先跑起来再说"；
- 预检原因码与 fail-closed 行为见
  [操作指南第 7 节](terminal-bench.md#7-预检与原因码)；
- 新组合转正需要：真实运行证据（原始产物或脱敏 fixture）、本文档新增一行、
  以及 `docs/verification/M3.md` 的对应记录。
