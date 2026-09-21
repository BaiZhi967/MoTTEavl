# M1–M3 真实验收清单（操作员执行）

> 性质：**执行清单**，不是验收通过记录。每一项完成后把 run id、模型 id/版本、
> 观察到的费用、终止原因与实际命令登记进对应的 `docs/verification/MN.md`，
> 才算取得该项证据。未执行项保持 `not_run` / `blocked`，不得以离线测试替代。
>
> 依据：`docs/verification/M1.md`、`M2.md`、`M3.md`；`docs/operations/native-agent.md`、
> `ceval.md`、`cmmlu.md`、`terminal-bench.md`、`harbor-compatibility.md`。
> 状态取值：`not_run` / `passed` / `failed` / `blocked` / `not_applicable`（skip 不计 passed）。

## 0. 三类验收与优先级

| 类 | 是否需要付费模型 | 项数 | 能否在本机（Windows）执行 |
|---|---|---|---|
| **A. 真实模型 live**（必须授权、会产生费用） | 是 | 5 | 否——需 Linux/macOS 宿主 |
| **B. 真实环境/任务集**（无模型调用或仅确定性 oracle） | 否 | 5 | 部分（PG 需 Docker） |
| **C. 平台/宿主变体**（环境矩阵补齐） | 否 | 3 | 否（需 linux/amd64 等） |

**建议顺序：先 B（零成本、可暴露链路问题）→ 再 A（付费，逐项收紧预算）。**
B 未通过时不应进入 A 的付费调用。

平台宿主前置：M1–M3 的全部绿色门禁产生于 **macOS arm64 + Docker 27.4.0**；
Windows 上 `uv run pytest -q -m "not live"` 会因 `openat`/`O_DIRECTORY`/symlink/`killpg`
等 POSIX-only 原语失败（实测 282 failed / 1290 passed），**不是回归**。
真实验收必须在 Linux/macOS 宿主或 WSL2 中进行。

---

## A. 真实模型 live 验收（需显式授权：任务清单、模型、Profile、重复数、预算、期限）

### A1. M1 原生 Agent 文件任务（真实 subject 模型）

- **解锁目标**：`M1-Supported`（当前"未满足"）。需**两个**已发布模型在同一文件任务上的
  真实运行 + 取消证据。
- **前置**：模型已 publish 且 `supports_tools=true`（native-tool 模式）；Provider 凭据已在
  `~/.motte/credentials.toml` 或环境变量。
- **预算说明（文档原文）**：每 case 每步一次模型调用（native-tool 默认 ≤8 步/case）；
  费用 = 输入（逐步增长的历史）+ 输出（≤模型上限）；无隐性调用。

```bash
# 1) 导入任务数据集（JSON 数组）
uv run python -m motte_cli agent-tasks import --file tasks.json --name file-report
# 2) 发布模型（或经 Web）
uv run python -m motte_cli models publish YOUR_MODEL
# 3) 创建 Run（对两个模型各做一次）
uv run python -m motte_cli agent-tasks run --scenario file-report@1 \
  --model YOUR_MODEL --mode native-tool --max-steps 8
# 4) Worker 执行（此处产生费用）
uv run python -m apps.worker.motte_worker --once
# 5) 读取结果
uv run python -m motte_cli runs show <run_id>
```

- **必须观察到的证据**：
  1. 两个模型各自的 run id、模型 id/版本、实际 usage 与费用、`termination` 停止原因；
  2. 多指标评分（文件断言通过 + 工具/文件证据完整）；
  3. 取消证据：运行中 `POST /api/v1/runs/{id}/cancel` → 无后续工具执行、清理结果可查
     （成功或残留清单）；
  4. 至少一次 `needs_review` 语义验证（如需，可用崩溃注入而非付费）。
- **登记位置**：`docs/verification/M1.md`（"操作者 live 验收准备"节下方），
  登记后可将对应模型标记为 `verified_in_live`。
- **已知边界（不因验收改变）**：workspace 是宿主本地目录 + 策略护栏，**不是容器隔离**；
  模型 token 流式不在 M1 范围。

### A2. M2 C-Eval 真实模型 smoke（小样本，授权预算）

- **前置**：① 固定 OpenCompass 0.4.2 Runner 已安装并注册
  （`scripts/runner/install-opencompass <dir>` + `var/runner/adapters.json` 或 `MOTTE_RUNNER_CONFIG`）；
  ② 数据已 prepare。**官方数据需先完成受信核验器部署**；未部署时只能用本地合法 JSONL，
  且会被标记为 `user-supplied`（不取得 official provenance，不冒充完整官方分数）。
- **模型侧**：Runner 原生调用（`transport_owner=runner-native`），
  未观测到的请求身份保持 unknown，不补填为已核验。

```bash
# 数据准备（来源治理门禁）
uv run python -m motte_cli ceval prepare \
  --files logic_val=./logic.jsonl --revision REV [--provenance user-supplied]
# 静态预检（0 模型调用）
uv run python -m motte_cli ceval preflight --model YOUR_MODEL --scope smoke --few-shot 0
# 创建 job-based Run（202 排队）
uv run python -m motte_cli ceval run --model YOUR_MODEL \
  --files logic_val=./logic.jsonl --revision REV --scope smoke
# 分派：一个 Run 只启动一个外部 Job
uv run python -m apps.worker.motte_worker --once
# Job 记录与结果
uv run python -m motte_cli runs show <run_id>
```

- **必须观察到的证据**：`scope=smoke` 标签始终随配置与报告出现；`native.*` 与
  `diagnostic.*` 双命名空间分开存储与显示；提取器/version 记录；未尝试的 selected case
  **不消失**（有明确 disposition）；费用未知保持 `unknown` + coverage，成本硬门禁不通过；
  原始 Artifact hash 可追溯；重复导入为 no-op、同键异内容为 conflict。
- **禁止**：把 smoke 范围写成完整 Profile 成绩；在无 gold 分区伪造正确率。

### A3. M2 C-Eval full Profile（选定完整集）

- 与 A2 同链路，额外硬条件：**选择集合必须与目标 Profile 完全一致**才可记为完整成绩
  （`scope=full` 要求覆盖分区全部行，入队前 422 校验）。
- 需确认官方数据 revision、许可记录与受信核验器；否则保持 `blocked`。

### A4. M2 CMMLU 真实模型 smoke / full（追加项）

- **独立验收**：CMMLU **不继承 C-Eval 的任何通过状态**，需独立记录数据、Profile 与证据。
  官方数据许可 CC-BY-NC-SA-4.0 的获取与分发约束由操作员核对。

```bash
uv run python -m motte_cli cmmlu prepare --files <subject>=./<file>.jsonl --revision REV
uv run python -m motte_cli cmmlu preflight --model YOUR_MODEL --scope smoke
uv run python -m motte_cli cmmlu run --model YOUR_MODEL --files ... --revision REV --scope smoke
uv run python -m apps.worker.motte_worker --once
```

### A5. M3 Terminal-Bench 真实 Agent 小批次（`claude-code`）

- **当前状态**：`claude-code` **实现与离线原生配置校验已完成**
  （`cli_version=2.0.30`、`ClaudeCodeOptions` 接受冻结配置、零容器零模型），
  但"真实调用通过"不在 M3 结论里 → 兼容矩阵按"实现完成 ≠ 组合已验证"登记。
- **授权清单（用户须逐项可审阅）**：任务清单、数据集 revision、模型、Profile、
  重复数（`n_trials ≤ 32`）、预算、期限。

```bash
# 只读准备
uv run python -m motte_cli terminal-bench prepare \
  --task-root DIR --source-id ID --revision REV \
  [--license-id L --license-evidence E --pinned-source]
# 只读预检（零模型调用、零任务启动）
uv run python -m motte_cli terminal-bench preflight \
  --agent-id claude-code --agent-version 2.0.30 --model MODEL_ID \
  --n-trials N --task-keys a,b --dataset-revision REV
# 创建 Run（凭据只登记引用，值来自 Runner 环境）
uv run python -m motte_cli terminal-bench run \
  --agent-id claude-code --agent-version 2.0.30 --model MODEL_ID \
  --credential-ref provider=env:ANTHROPIC_API_KEY \
  --n-trials N --aggregation mean-success --task-keys a,b \
  --agent-timeout-sec S --agent-setup-timeout-sec S \
  --verifier-timeout-sec S --job-timeout-sec S
uv run python -m apps.worker.motte_worker --once
# 结果与下钻
uv run python -m motte_cli terminal-bench status --run-id RUN [--json]
uv run python -m motte_cli terminal-bench trials --run-id RUN --task-key KEY
uv run python -m motte_cli terminal-bench trial --run-id RUN --trial-id ID
```

- **必须观察到的证据**：每个计划 Trial 都有 disposition（不因一条非法 Trial 中止整批）；
  `reward=0` 是**有效失败**而非平台错误；缺 reward / 畸形 / Verifier 错误三态区分；
  通过率与覆盖带明确分母与单位；Agent / Verifier / 环境时长**分相**；成本未知保持
  `null`、零成功单位成本为"不适用"；运行后 `docker ps -aq --filter label=motte.job` 为空；
  操作员 retry 创建**新 Run**（不覆盖 Trial）。
- **注意**：`--credential-ref NAME=env:VAR` 只登记引用，变量缺失时 Runner 在启动前
  以退出码 5 拒绝。

---

## B. 真实环境 / 任务集验收（无需付费模型）

### B1. M3 上游完整任务集（最高价值、当前 `not_run`）

- `terminal-bench` 2.0（**89 题**，commit `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c`）
  与 `terminal-bench-sample` 2.0（**10 题**，commit `7e917f35c281188532772312d4ad91ca9274febc`）。
- 当前只有准备/身份路径有夹具测试，**完整集从未下载运行**。
- 用 `oracle` Agent 跑（零模型、零凭据、零成本），验证 Task 身份、准备只读、
  计划分母覆盖、部分失败处置、资源清理。
- 完成后在 `docs/operations/harbor-compatibility.md` 新增行并同步 `docs/verification/M3.md`。

### B2. M3 真实运行中的取消与容器级残留核查

- 状态：原 `not_run`，已在 review round-1 用 `motte` 自有夹具**实跑收口**
  （运行中取消 + 诱饵容器 + 冒名容器：只停真正拥有的容器）。
- **待补**：在**上游真实任务**上的取消与残留核查（B1 的子项）。

### B3. M2 真实固定 Runner + 官方 Profile / native accuracy 对照

- 本地确定性端点已"已验证：本地推理与平台评分"，但**官方完整 Profile 与
  native accuracy 对照未验证**（当前无 native accuracy 时不宣称 native 对照通过）。
- 需官方数据 revision + 受信核验器。

### B4. 真实 PostgreSQL 实机复验

- 已在本机一次性容器实跑（M1 19 passed；M2 not_run→CI；M3 1346 / 1454 / 1521 passed）。
- **待补**：在**生产/准生产 PG 实例**上的迁移链 `0001 → 0008` up/down/再 up、
  两连接并发首写幂等/冲突、降级守护。
- **注意**：平台无认证/角色体系，二进制证据原始导出**默认拒绝**
  （`ARTIFACT_RAW_EXPORT_DISABLED`），只有操作员显式设置
  `MOTTE_ALLOW_RAW_ARTIFACT_EXPORT=1` 才返回原始字节——这是"默认拒绝 + 操作员开关"，
  **不是按调用者授权的访问控制**。

### B5. M1 真实 Docker 全链路复验

- M1 已在一次性 `postgres:16-alpine` 上验证（alembic 0001→0005，19 passed）；
  如需在目标环境重跑，命令见 `docs/verification/M1.md`。

---

## C. 平台 / 宿主变体（环境矩阵补齐）

| 项 | 状态 | 说明 |
|---|---|---|
| C1 `linux/amd64` 宿主 | `not_run` | 本机为 arm64 引擎；Linux 部署须重复 OpenCompass 依赖验收（0.4.2 的 `pandas==1.5.3` 与 Python 3.12 不兼容，故 Runner 用独立 3.10.20） |
| C2 受限网络策略（`none`/`restricted`） | `not_run` | 会产生 `platform_custom_profile: true`，**不得继续冒充完全同口径** |
| C3 Verifier 可见性变体（非 `upstream`） | `not_run` | 同上，必须标平台自定义安全配置 |

---

## D. 每项验收必须登记的证据字段

```yaml
milestone: M1 | M2 | M3
item: A1..A5 | B1..B5 | C1..C3
implementation_commit: <HEAD sha>
verification_layer: live | integration | contract | release
result: passed | failed | blocked | not_run
command: <实际执行的完整命令>
environment: <宿主/内核/arch、Docker 版本、Runner 包版本与锁文件、PG 版本>
evidence_refs: [<run_id>, <artifact hash>, <log path>]
authorization: <PaidCalls/预算上限/期限的授权记录>
limitations: []
```

**判定纪律（各阶段文档原话）**：失败、跳过、缺环境、未授权均不能记 `passed`；
`skip` 不作为通过证据；"实现完成"不等于"组合已验证"；
GET / report / compare / gate **不触发**新的付费调用。

## E. 与阶段结论的关系

| 阶段 | 当前结论 | 本清单哪些项能改变它 |
|---|---|---|
| M1 | Core 满足；**Supported 未满足** | A1 → 可宣称实时支持真实 Agent |
| M2 | 代码完成、**验收未收口** | A2/A3/A4 + B3 → 阶段退出条件满足 |
| M3 | 代码检查通过、**阶段验收未完成** | A5 + B1 + B2 → 层 3 与上游任务集收口 |

未完成 A/B/C 之前，不得声明"C-Eval 已可用""Harbor 一切任务已支持"或
"v1 已完成"——范围删减须写明原因、影响、保留接口与后续条件。
