# MoTTEavl M1-M7 实际测试报告

- 测试日期：2026-09-22（UTC+08:00）
- 代码基线：`main` @ `01d347681d5ff88ee2bfd9d83cc015f6d2b7cef3`
- 测试环境：Windows 11 / PowerShell 5.1 / Python 3.12 / uv 0.12.6 / Node 24.18.0 / pnpm 9.15.0
- Web/API：`make dev`，API `http://127.0.0.1:8000`，Web `http://127.0.0.1:5173`
- 模型：已配置的 `deepseek-v4.1-flash`（Provider `6a`）
- 浏览器工具：browser-skill Agent Window
- 配套文档：[代码与功能审查](2026-09-22-m1-m7-code-feature-review.md)、[详细测试点](2026-09-22-m1-m7-test-points.md)

> 第 1-11 节保留首次审查的测试事实；修复后的 superseding 结果见第 12 节。

## 1. 总结

**测试结论：核心 Direct LLM 与 Builtin Agent live 链路通过；Web/CLI 主路径可运行；发布门禁不通过。**

通过项：

- API/Web 正常启动，health 200。
- DeepSeek 模型真实连通性测试通过。
- CLI 单题 Direct LLM：导入、创建、Worker、事件、报告 local/server 均可执行，评分通过。
- Web 单题 Direct LLM：估算、提交、排队、Worker、实时状态、结果、逐题证据均通过。
- CLI + Web Agent native-tool：真实读取/写入工具链、产物采集、多指标评分、事件下钻均通过。
- Web 21 个测试文件 / 286 项测试通过；production build 通过。
- ruff、mypy、compileall、OpenAPI drift check 通过。

不通过/阻断项：

- `make check` 失败：237 failed / 2606 passed / 81 skipped / 1 deselected。
- Docker 不在 PATH，`make compose-config` 失败；Compose build/up 与 Harbor live 未执行。
- Judge Web 调用真实 API 返回 405，整页不可用。
- Harness 目录真实 Uvicorn 请求在 Windows 返回 500。
- 安全/数据完整性定向反例发现 Provider redirect 泄密、Origin 绕过、GC 引用漏遍历、Artifact 路径逃逸、SSE 500 条截断等发布阻断缺陷。

## 2. 自动化门禁

| 项目 | 命令 | 结果 | 备注 |
|---|---|---|---|
| Ruff | `uv run ruff check .`（由 make check 执行） | PASS | All checks passed |
| Mypy | `uv run mypy packages/contracts` | PASS | 33 source files，0 issue |
| Compile | `uv run python -m compileall -q packages apps` | PASS | 无错误 |
| Python 全量 | `uv run pytest -q -m "not live"` | **FAIL** | 2606 passed / 237 failed / 81 skipped / 1 deselected；27m57s |
| Web tests | `pnpm --dir apps/web test` | PASS | 21 files / 286 tests；231.38s |
| Web build | `pnpm --dir apps/web build` | PASS | 4690 modules；9.62s |
| Bundle | Vite build output | WARN | JS 849.09 kB，gzip 236.63 kB，超过 500 kB warning |
| OpenAPI | `make openapi-check` | PASS | `openapi.json is up to date` |
| Compose | `make compose-config` | **BLOCKED/FAIL** | 系统找不到 docker executable |
| make check | `make check` | **FAIL** | 在 Python pytest 阶段停止，后续由本轮单独执行 |

### 2.1 Python 失败口径

本轮结果比 M7 验证账本记录的 232 failed / 2611 passed 多 5 个失败、少 5 个通过，总收集规模一致。未保存历史 node-id 清单，不能证明这 5 项一定是新增回归，也不能把 237 项全部当作环境噪声。

大量失败堆栈仍集中于：

- `packages/benchmark-runtime/motte_benchmark/trusted.py:43` 在 Windows 对目录 fd 打开返回 Permission denied。
- POSIX symlink/进程组/外部 Job 恢复语义在 Windows 不可直接运行。
- M2/M3 frozen evidence/adapter 测试由上述平台错误级联失败。

正式结论：Windows 当前不满足全量门禁；Linux/PG CI 需要另行确认，但不能替代 Windows 支持矩阵中的真实状态。

## 3. 服务启动与基础可用性

1. 执行 `make dev`。
2. API 启动并通过 `GET /health` 200。
3. Web Vite 在 5173 启动，API proxy 正常。
4. 服务由 `apps.dev` 统一监管；测试结束后已停止。

## 4. 模型真实连通性

在 Web 的 Provider 与模型页对 `deepseek-v4.1-flash` 点击“测试”：

- 结果：PASS
- 延迟：719 ms
- usage：输入 36 / 输出 16 tokens
- UI 正确从“测试中”更新为“通过”
- 未发现明文密钥回显，页面只显示掩码

此项只证明当前 endpoint/key/model 的一次连通性，不证明 redirect 安全、自动重试安全或长期稳定性。

## 5. CLI 真实测试

### 5.1 构造数据

创建一条合成 JSONL：

- 临时路径：`var/review/m1-m7-audit/direct-llm-smoke.jsonl`（测试后已删除，Run 与数据库审计证据保留）
- Case：`audit-exact-001`
- Prompt：要求只输出字符 `4`
- Expected：`4`
- Scorer：`exact`

通过 CLI 导入为 `audit-direct-llm-smoke@1`，导入结果为 1 case，并生成 source/cases/dataset fingerprint。

### 5.2 Direct LLM CLI Run

命令流程：

1. `motte direct-llm import --file ...`
2. `motte direct-llm run --scenario audit-direct-llm-smoke@1 --model deepseek-v4.1-flash --case-ids audit-exact-001 --temperature 0 --max-output-tokens 32 --reasoning-level none`
3. `python -m apps.worker.motte_worker --once`
4. `motte run-get/run-events/run-report`，分别验证 local 与 server transport

结果：

- Run：`run-89705c326ca0437c9146d4215cd7408d`
- 状态：completed
- 模型输出：`4`
- exact：PASS
- 模型延迟：891 ms
- usage：prompt 16 / completion 1 / total 17
- 事件：queued -> preparing -> running -> model_response -> collecting -> scoring -> score -> scoring_pass_created -> completed
- Worker stdout 保持空，结构化进度写 stderr，退出码 0

### 5.3 CLI local/server 对账

通过项：

- `run-get` local/server 均返回同一 Run、终态和 Case。
- `run-events --snapshot` server 返回完整 9 个 Direct LLM 事件。
- `run-report` local/server 核心 score/case 判定一致。

缺陷：

- server report 包含 aggregation、cost、usage、benchmark 等丰富字段；local report 是较小的自建 shape，机器输出不是“仅 transport 不同”。
- 静态/定向反例进一步确认 server `run-events` 对 500+ 事件只取一页，而 local 返回全部。

## 6. Web 真实 Direct LLM 测试

Browser-skill 操作：

1. 打开 Direct LLM 操作页。
2. 选择 `audit-direct-llm-smoke@1`。
3. 选择 `deepseek-v4.1-flash`，reasoning=none，temperature=0，max_output_tokens=32。
4. 点击“运行前估算”。
5. 确认估算为 1 case、输入上界 107、输出上限 32、总量上界 139，且没有创建 Run。
6. 点击“发起评测”。
7. 页面进入 queued，显示等待 Worker。
8. 启动 Worker `--once`。
9. 页面通过事件更新为 completed 1/1。
10. 打开结果并展开 Case。

结果：

- Run：`run-6f32a5badaed49b2a6bf00e044c54e84`
- 状态：completed
- 通过率：100%，1/1
- usage：17 tokens（16+1）
- 模型身份：requested/reported/actual 均为 `deepseek-v4.1-flash`
- identity policy：report_only，判定完全一致
- Case 下钻显示 prompt、模型输出 `4`、expected `4`、scorer 版本和 source_line

## 7. Agent native-tool 真实测试

### 7.1 CLI 创建和 Worker 执行

使用 CLI 创建一条受限任务：

- Scenario：`acc-file-report@1`
- Case：`acc-001`
- Model：`deepseek-v4.1-flash`
- Mode：`native-tool`
- Budget：max_steps=8、max_tool_calls=16、wall_time=120s

Run：`run-0714e839052d4ff2aeb4fe1a308e0be1`

Worker 结果：

- completed，退出码 0
- Case 执行约 5.16s；Agent 记录 duration 4844ms
- 3 steps / 2 tool calls
- 顺序：read_file(input.json) -> write_file(report.json) -> final_answer
- 输出产物：`{"enabled_count": 2}`
- 指标 `file-content:report.json`：PASS
- 指标 `no-forbidden-write`：PASS
- cleanup：success，无 residual
- 事件数：25，包含 model_request/response、tool_call/result、final_answer、terminated 与 ScoringPass

### 7.2 Web 下钻

刷新 Agent 文件任务页后，新 Run 出现在最近运行首行。结果页正确展示：

- 状态 completed、mode native-tool、终止原因“模型给出最终回答”
- 两项评分均为“已评分/通过”
- 可选择 `acc-001` 下钻
- 可查看 `input.json` 与 `report.json`，后者内容为 `{"enabled_count": 2}`
- 事件轨迹显示 read/write 工具链与 final_answer

发现：报告顶层 usage 为空、cost unknown；Case observation 内有 total_tokens=2217，但逐调用 usage 的 token 字段被通用键名脱敏为 `[REDACTED]`。这会削弱 M1 的费用/用量审计证据。

## 8. Web 全导航巡检

通过 browser-skill 逐页打开：

- Agent 文件任务
- CMMLU
- Terminal-Bench
- C-Eval
- GSM8K
- Direct LLM
- Replay
- 外部 Runtime
- 运行总览
- Provider 与模型
- Agent / Harness
- 场景 Workflow
- Skill 校验
- Judge 校准
- 实验
- 比较
- 基线
- 门禁

正常表现：

- 所有路由均有语义 DOM，无空白页。
- C-Eval/CMMLU 对未准备数据和未接 Runner 显示明确 blocker。
- Terminal-Bench 在未准备 task set 时显示“暂无已准备的任务集”。
- Baseline/Gate 空态和创建表单可见。
- Direct LLM 与 Agent 的监控、结果和下钻可用。

失败/问题：

| 页面/接口 | 结果 | 判定 |
|---|---|---|
| Judge | GET `/api/v1/judges` -> 405，页面显示能力不可用 | FAIL，Web/API 契约断裂 |
| Harness | GET `/api/v1/harnesses` -> 500，Uvicorn 堆栈为 `asyncio.create_subprocess_exec` NotImplementedError | FAIL，Windows 真实服务不可用 |
| Terminal tasks | GET tasks -> 422 `DATASET_UNPREPARED` | 预期前置未满足；UI 能显示空态，但 console/network 有 422 |
| Agent catalog | builtin-react 仍写“尚未接入 ExecutionBackend”/execution_ready=false | 文案与真实能力冲突 |
| 移动布局 | 375/窄窗口仍保留固定侧栏和主内容 | FAIL，可达内容被裁剪；无响应式导航 |

浏览器 console 还记录了上述 405/500/422。Desktop screenshot 可正常获取；窄屏 screenshot 工具连续超时，但 accessibility/viewport 观察已确认固定 sidebar 与内容同时存在。

## 9. 定向安全与边界反例

以下由只读代码审查配合合成数据/本地服务定向复现，不使用真实密钥：

| 反例 | 实际结果 | 判定 |
|---|---|---|
| Provider 302 到第二 origin | 第二 origin 收到合成 Authorization | CRITICAL FAIL |
| Origin allowlist 为 localhost:3080，写请求来自 localhost:9999/https:4443 | 仍返回 202 | HIGH FAIL |
| 终态 Run 持久化 526 事件 | SSE/server CLI 仅输出前 500 | HIGH FAIL |
| Provider 首请求已收 body、客户端 Timeout | 同 body 被自动重放 | HIGH FAIL |
| OpenAI Responses/Anthropic 第二轮 canonical tool history | name=None/arguments 为空 | HIGH FAIL |
| ToolRegistry mode=replay/typo | 真实 handler 仍执行并产生副作用 | HIGH FAIL |
| ProcessRunner task cancellation | child PID 仍存活 | HIGH FAIL |
| Direct LLM 约 2000 层 JSON | 未捕获 RecursionError | HIGH FAIL |
| 并发 Anthropic stream | usage/stop reason 可串到另一调用 | HIGH FAIL |

其余 GC、ArtifactStore、PG 并发、maintenance、Docker workspace 问题已通过源码控制流确认，但本轮未在真实破坏性环境执行 apply/recovery；状态为“confirmed by code review, regression test required”。

## 10. 未执行与限制

- 无 Docker：未执行 Compose build/up、DockerSandbox live、Harbor oracle/真实任务。
- 未配置本轮真实 PG DSN：未执行 PG 双连接并发、pg_dump/restore、M7 staging restore。
- 未取得官方 C-Eval/CMMLU 数据与完整许可：未执行 full Profile。
- 未执行 Pi/Claude/Codex/app-server 的真实外部任务。
- 未执行 Judge >=30 条人工校准。
- 未执行真实旧平台导出 apply/resume/rollback。
- 未执行 M7 三个替代场景与正式 cutover。
- M1 本轮只执行 DeepSeek 单模型正常 case；第二模型、取消、预算触顶、needs_review 仍未跑。
- 未对 237 个 Python 失败与历史 232 个失败做精确 node-id 差分。

## 11. 最终结果

| 维度 | 结论 |
|---|---|
| Web 核心 Direct LLM/Agent | PASS（有界 live） |
| CLI 核心 Direct LLM/Agent | PASS（有界 live） |
| Web 全产品入口 | PARTIAL（Judge/Harness 失败，移动端不可用） |
| Web 自动化/build | PASS |
| Python 全量门禁 | FAIL |
| Docker/Compose | BLOCKED |
| 真实 PG/迁移恢复 | NOT RUN |
| 安全/数据完整性 | FAIL（存在 CRITICAL/HIGH） |
| stable_supported | NO |
| cutover_ready | NO |

**发布建议：阻断 RC/cutover。先修复 Provider redirect、Artifact/GC、Origin、maintenance/backup、SSE 分页和 Judge/Harness 契约，并补对应反例测试；之后再重跑全量门禁和外部场景。**

## 12. 修复后复验（superseding）

### 12.1 自动化结果

| 范围 | 结果 |
|---|---|
| 修复子系统合并回归（Provider/Evaluator/Agent/Scenario/Storage/PG/Sandbox/API/CLI/Harness） | `469 passed, 35 skipped` |
| Windows TrustedDir/Harbor frozen bytes/TerminalBench fingerprint | `41 passed, 1 skipped`；skip 为本机无 symlink privilege |
| Windows Runner env/外部 Job 生命周期/持久化取消 | `23 passed, 1 skipped`；取消竞态另以 thread-warning-as-error 通过 |
| Web 全量 | 21 files，`288 passed` |
| Web production build | PASS；仅保留 >500 kB chunk size warning |
| Ruff / mypy / compileall | PASS；33 个 contracts source 无 issue |
| OpenAPI drift | PASS；`api/openapi.json` 与 `schema.d.ts` 已重新生成 |
| Python 完整 `not live` | **FAIL**：`14 failed, 2860 passed, 97 skipped, 1 deselected`，2 warnings，19m16s |
| 最终修复合并回归 | `92 passed, 1 skipped`；覆盖 Judge/CLI/report/Pi/TerminalBench/Windows Job/Harbor mount/TrustedDir |
| Compose config | BLOCKED；本机找不到 Docker executable |
| PostgreSQL live | NOT RUN；未设置 `MOTTE_PG_DSN` |

### 12.2 有界真实 Provider/CLI/API 验证

使用已发布模型资源 `deepseek-v4.1-flash`（Provider `6a`）创建并执行一题 Direct LLM Run：

- Run：`run-2550a844d8ec476b853a979394dfea6b`；case：`audit-exact-001`。
- Worker 完成 `preparing -> running -> collecting -> scoring -> completed`，一次调用，无重试。
- Provider 返回内容 `4`，reported model 精确匹配；42 prompt tokens、25 completion tokens、67 total tokens，latency 875 ms。
- exact scorer 通过，accuracy/pass rate/completion/attempt rate 均为 1.0。
- server CLI 成功读取 canonical report 和完整 9 条事件，终态为 `completed`。

### 12.3 Web/API 实际验证

- `make dev` 启动 API/Web，`/health` 返回 200；`/api/v1/harnesses` 返回 2 项，JudgeSpec 目录返回 6 项。
- 最终 live smoke 确认全部 Judge 为 `available`、校准状态为 `not_run`、rubric 均含 canonical `criterion_id`。
- server CLI 在 transport/business `--mode` 分离修复后成功读取既有 DeepSeek Run `run-2550a844d8ec476b853a979394dfea6b`，状态为 `completed`。
- browser-skill 桌面页进入 Judge 清单，真实读取到 6 个 published spec；该操作发现了真实 response 使用 `criterion_id`、目录校准状态使用未登记值的问题。
- 修复后 API 使用 `not_run`，Web 同时以 canonical `criterion_id` 渲染；Web fixture 改为真实字段并通过 9 项 Judge 测试。
- Judge 只读预检携带从 published spec 推导的有界 preview authorisation，服务端不再 422；真实付费提交仍要求显式确认和幂等键。
- 终态 Judge job 可显式“新建请求”，不会永久锁死页面；运行中 job 仍保留监控/取消上下文。
- Judge 目录复用 provider snapshot resolver，仅公布 enabled 且支持 completion 的 model/provider，排除 disabled 与 replay-only 资源。
- 浏览器插件在运行上下文刷新后不再暴露绑定，因此修复后的真实桌面复看和移动视口复看未完成；这两项保持 `not_reverified`，由组件测试/build 不能替代。

### 12.4 新增 Windows 跨平台修复

- `TrustedDir`：Windows 不支持目录 fd，改为逐组件 `lstat` + resolved containment，并拒绝根 symlink/junction。
- Harbor frozen copy：低层写入加入 `O_BINARY`，避免 LF 被改写为 CRLF 后摘要漂移。
- Runner env：白名单补齐 `SystemRoot`/`WINDIR`/`ComSpec`/`PATHEXT`/`USERPROFILE`，避免 Winsock provider 初始化失败。
- ProcessJobAdapter：Windows 通过命令行 token 复核 owner，使用 `taskkill /T /F` 终止整棵进程树并释放 transport handle。
- RunService：operator cancellation 与 collecting/scoring transition 竞态改为终态复核，不复活 Run、不抛后台线程异常。
- CLI：transport `--mode local/server` 使用独立 argparse destination，不再覆盖 Agent 的 `--mode native-tool/legacy-json`。
- Harbor：Windows 盘符挂载 `C:\\...:/container` 按 bind source 解析，敏感宿主目录不再绕过预检；compose 短语法同口径。
- Pi：InvocationRecord 恢复可审计 `metering_source`；API 内部 `_build_report` 保留兼容别名，同时 Web/CLI 仍共用 canonical report builder。

完整套件剩余 14 项中，13 项在测试夹具调用 `symlink_to/os.symlink` 时因本机缺少 Windows symlink privilege（`WinError 1314`）终止，未进入被测拒绝逻辑；另 1 项 Harbor fixture cleanup 因 Docker 不可用返回保守 `unknown`，测试原期望 `residual`。它们均不在最终 92 项修复回归的失败列表中，但仍按门禁 FAIL 记录，不能写成通过。

### 12.5 修复后结论

| 维度 | 当前结论 |
|---|---|
| 已审查 CRITICAL/HIGH | PASS（实现 + 定向回归；PG/Docker 条目仅 offline） |
| 关键 MEDIUM | PASS（定向回归） |
| DeepSeek Direct LLM | PASS（有界 live） |
| CLI/API report/events | PASS（有界 live） |
| Web 自动化/build | PASS |
| Web browser | PARTIAL（修复前目录实测；修复后/mobile 未复验） |
| Python 全量门禁 | FAIL（见 12.1） |
| Docker/Harbor live | BLOCKED |
| PostgreSQL live/backup restore | NOT RUN |
| Judge 人工校准 / 外部场景 / cutover | NOT RUN |
| `stable_supported` | NO |
| `cutover_ready` | NO |

**发布建议：已不存在本次审查中未修复的 CRITICAL/HIGH 代码 finding；仍阻断 RC/cutover，直到 Python 全量剩余失败归零，并在具备 Docker、PostgreSQL、官方数据与人工校准环境中补齐 live 证据。**
