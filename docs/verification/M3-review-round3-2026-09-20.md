# M3 实现 Review — Round 3（2026-09-20）

## 结论与范围

**仍不通过：2 项 P1、5 项 P2，共 7 项反馈。** 上轮正常执行中的取消、retry、外来结果隔离、合法兄弟结果保留、模型比较政策等反例已通过复验。本轮剩余问题集中在 Compose 的实际解析语义、所有权 fallback、公共模型参数、文本截断和 Trial 终态/输入边界。

- 基线：`046fa5f`；审查提交：`33767bf20cebbfe7fad4bbe47d4af9e6152cb4b9`。
- 分支：`codex/m3-harbor-terminal-bench`；开始时工作区干净。
- 对照：[第二轮报告](M3-review-round2-2026-09-20.md)、[第二轮修复记录](M3-review-round2-fixes-2026-09-20.md)、[M3 需求](../roadmap/M3-harbor-and-terminal-bench.md)。
- 主审复核服务、存储和比较；两个独立只读子代理复核 Runtime 安全与公共 API/Web/Agent 配置。本轮仅新增此报告，不修改业务代码、不提交推送。

## 验证结果

重新运行 `make check`，退出码 0：Python **1503 passed / 45 skipped / 2 warnings**；Web **15 files / 223 tests passed**；其他 lint、类型检查、构建、Compose 配置与 OpenAPI 门禁通过。日志：`/tmp/motte-m3-round3-make-check.log`。

另执行公共 API/内容读取/Agent 映射定向回归 **58 passed**，Compose 与环境白名单定向回归 **13 passed**。它们与全量门禁重叠，不相加计算测试总数。

重跑上一轮补充反例得到：

| 场景 | 本轮实际结果 |
|---|---|
| 正常 2 Task × 2 Trial | completed，4 Trial，4 scores |
| job_entry 返回前请求取消 | cancelled，4 Trial，4 scores；已采集结果不再丢失 |
| retry 经 DurableExternalJobRunner + fixture adapter | 子计划 run_id 正确，子 Run completed，4 Trial，4 scores |
| 冻结计划正常，outcome 携带另一 Run / 不存在 Trial ID | failed，外来结果被隔离，其他 Run 原记录不变 |
| 一条 payload 的 run_id 错误 | failed，3 条合法结果保留，另 1 条 indeterminate 占位，4 条评分行 |
| 比较政策不允许模型变化 | eligible=false，FACTOR_NOT_ALLOWED:model |

证据仍分层：本轮使用临时 SQLite、fixture Runner、假 Docker client、合成秘密与纯函数；**没有执行真实 Provider、真实 Docker Job/清理、真实 PG 或浏览器交互验收**。开发方已有真实环境记录不冒充本轮独立复验，skip 不计通过。

## P1

### M3-R3-01 — 插值默认值不代表实际挂载路径，HOME 仍可绕过预检

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/compose.py:201`–203；`packages/benchmark-runtime/motte_benchmark/env_boundary.py:44` 的环境白名单。
- **复现：** 合法任务的 Compose 使用 `volumes: ["${HOME:-./data}:/host"]`。公共 prepare → preflight 返回 `allowed=true`、空 reason_codes；独立 `plan_process_env` 证明 HOME 被保留到 Runner 环境。
- **根因/影响：** `_expand_worst_case` 不检查变量是否存在，直接拿默认 `./data` 作为路径判定。执行时已有 HOME 优先，实际挂载的是 Runner 的宿主 home，而不是任务内目录。白名单保留的变量同样需要参与有效路径解析。
- **修复与复验：** 预检与执行使用同一份冻结、可复核的插值环境，覆盖允许继承的变量与 Compose 环境文件；不能确定最终来源时拒绝相关表达式。加入“安全默认值 + 实际危险变量值”的反例，不能只测试默认值本身为 `/`。
- **证据边界：** 公共预检和环境规划已复现，未实际执行危险挂载。实际优先级依据 [Docker 插值与环境优先级](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)。上轮 R2-04 未完整闭环。

### M3-R3-02 — env_file 内容未检查，可将 Agent 凭据转发给任务

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/compose.py:517`–524；调用点 `:676`–680。
- **复现：** Compose 声明 `env_file: ./task.env`，任务内该文件内容为 `COPY=${ANTHROPIC_API_KEY}`。公共 prepare → preflight 返回 `allowed=true`、空 reason_codes。合成环境验证显示，Profile 声明的 ANTHROPIC_API_KEY 仍会进入 Runner 环境。
- **根因/影响：** env_file 只校验路径在任务内，没有受控读取其内容；凭据检查只覆盖 YAML 的 environment。Compose 默认会对 env_file 的相应值插值，因此任务可通过普通变量 COPY 获得 Runner 的 Agent 凭据。
- **修复与复验：** 检查 env_file 内容及插值依赖，或拒绝不能证明安全的环境文件形式。回归必须覆盖最终有效任务环境，而非只检查 YAML 中是否出现敏感变量名。保持可信 Agent 凭据注入可用，禁止任务自行转发。
- **证据边界：** 使用合成秘密，未读真实密钥、未启动容器。解析行为依据 [Docker env_file 语义](https://docs.docker.com/reference/compose-file/services/#env_file)。上轮 R2-05 未完整闭环。

## P2

### M3-R3-03 — project fallback 仍可覆盖明确的 Run/owner 冲突

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/containers.py:147`–165。
- **复现：** 假容器具有匹配的 compose project，缺少 motte.job，但 motte.run、motte.owner 明确指向另一个 Run/owner。classify 返回 match；stop_owned(remove=True) 实际调用 fake 容器的 stop/remove，最终返回 clean。
- **根因：** Run/owner 的冲突判断仍放在 `job == self.job_id` 分支中。缺少 job 标签时，project fallback 直接认可匹配，忽略已有的相反证据。
- **修复与复验：** 对每个候选容器先独立检查所有已有身份字段的冲突，再评估正向匹配。旧标签缺失兼容不能吞掉明确冲突。补“无 job + project 命中 + owner/run 冲突”的诱饵测试。
- **边界：** 本轮没有停止真实容器。上轮 R2-08 原 job 命中反例已修，fallback 变体未闭环。

### M3-R3-04 — API/CLI 丢弃已发布模型参数，绕过新增执行校验

- **定位：** `apps/api/app/main.py:2130`–2133；`packages/cli/motte_cli/main.py:244`–247。
- **复现：** 经真实临时 API 创建模型，parameters 为 `{"temperature":0.23,"max_output_tokens":789}`，创建返回 201、发布返回 200。选择该模型创建 Claude Agent Run 返回 202，但冻结 profile.model 仅包含 provider/model，原生 kwargs 仅有 version。
- **根因/影响：** 这些是 `ParameterProfile` 正式允许的字段，公共入口解析已发布模型时先将它们丢弃，导致新 `execution_option_plan` 根本看不到它们。SDK 直接接收不支持参数时会拒绝，公共入口却假装接受用户选中的完整模型配置。
- **修复与复验：** API/CLI 冻结已发布模型的完整相关执行参数，再统一映射或具名拒绝。对该固定 Agent 不支持的参数返回明确错误，不能静默使用默认值。增加“发布非空参数 → 公共创建 → 原生配置/明确拒绝”的测试。
- **关联：** R2-11 的 SDK 映射已修，公共模型消费未闭环。

### M3-R3-05 — 新 UTF-8 截断器仍会误判合法文本或超过预算

- **定位：** `packages/sdk-python/motte_sdk/terminalbench.py:776`–788。
- **复现：** 默认 65536 字节预算，正式 read_artifact_text：
  - `("a" * 65535 + "中" + "z" * 5000).encode()` → encoding=binary、text=None；
  - `("a" * 65533 + "🚀zz").encode()` → 返回正文 65539 字节，超过预算。
- **根因：** 剩余预算按已输出字符的字节数计算，没有扣除 decoder 中暂存的输入字节；截短 chunk 后仍按 4096 推进源索引，可能跳过必要续字节。
- **修复与复验：** 先限定输入为 `data[:max_bytes]` 再增量解码，或独立维护正确的输入消费计数；结尾不完整字符只舍弃尾部，不应将整个合法文件标成 binary。覆盖长尾、中文和 emoji 的所有预算切点，并断言输出字节数不超限。
- **关联：** R2-13 原短尾用例通过，其他合法边界仍失败。

### M3-R3-06 — 排队取消与隔离失败仍没有完整 Trial 终态处置

- **定位：** `packages/sdk-python/motte_sdk/service.py:305` 的计划落库时机、`:1432` 的 `_ensure_trial_plans`、`:1470` 附近的 `_dispose_rejected_trials` 与 `_finish_unattempted`。
- **复现 A（常规操作）：** create_run 创建含 4 Trial 计划的 queued Run，尚未 dispatch 就 cancel。最终 cancelled，但 Trial repository 为 0 条、评分为 0 条。计划仍在 manifest 中，故不是计划分母从 manifest 丢失，而是公共 Trial 记录/处置不存在。
- **复现 B：** 正常冻结计划收到外来/未知 Trial ID，Run 正确 failed 并隔离结果，但自己的两个计划 Trial 永久保持 pending。当前新增测试甚至以 result=None 为预期，未检验终态 disposition。
- **根因：** 计划只在 execute_external_job 开始时落库；提前取消路径没有调用该逻辑。拒绝补处置只覆盖 importer.invalid，不覆盖映射阶段 unmapped，普通 failed 终态也未统一结清计划单元。
- **修复与复验：** 创建/终态化时确保计划存在；Run 终态前为未确定单元给出 cancelled/not_attempted/indeterminate 等准确处置，保留未知身份的隔离证据，不猜测归属、不覆盖已有真实结果。分别测试 queued cancel、错误身份隔离、启动失败。
- **关联：** 执行中取消已修；R2-01/03/09 的“全部计划单元有处置”边界仍不完整，涉及 M3-G10。

### M3-R3-07 — 冻结计划自身属于另一 Run 时，导入边界仍不拒绝

- **定位：** `packages/sdk-python/motte_sdk/service.py:1447`–1451；`packages/sdk-python/motte_sdk/benchmark_plugins.py:303`–304、`:327`–340。
- **前提与复现：** 通过 SDK 给 run-b 创建一份仍带 run-a TrialPlan 的 manifest；run-a 的合法计划已存在。执行 run-b 时 outcome 使用该 manifest 内的 Trial ID，服务接受并完成，run-b 为 0 Trial/0 scores，run-a 的一条 Trial 被写成 succeeded。
- **根因：** 当前只验证 ID 是否在 manifest 计划中，没有验证每条计划的 run_id 是否等于当前 Run ID。importer 虽读出了 run_id，但没有用它核对 frozen/存储记录；因此无法识别“整份输入计划错绑”，只识别“结果 ID 不在计划中”。
- **修复与复验：** 在任何 create_plans/put_result 和任务启动之前验证全部 TrialPlan 与当前 Run 一致；importer 再做防御性核对。错绑输入应明确拒绝，两个 Run 的既有记录均不改变。
- **边界与优先级：** 这是带错误冻结 manifest 的公开 SDK 反例，**不是**当前正常 API 创建或已修复 retry 会自动生成该错误。本轮按 P2 输入契约/持久化保护缺口记录，不将它描述为 HTTP 跨 Run 攻击。R2-03 对单条外来结果的原反例已修。

## 上轮 13 项的复核状态

| 上轮编号 | 本轮判断 |
|---|---|
| R2-01 | 执行中延迟/立即取消、保留已确定结果原用例通过；排队取消处置见 R3-06。 |
| R2-02 | 子 Run/Job/Trial 身份重新派生通过实际 fixture Runner 复验；原问题关闭。 |
| R2-03 | 正常计划下的外来/未知 ID 被隔离，其他 Run 不变；错绑计划输入与终态处置见 R3-07/06。 |
| R2-04 | 命名资源、危险默认路径等原反例已拒绝；实际环境值优先于默认值的绕过见 R3-01。 |
| R2-05 | 直接列表/null 透传、平台环境白名单原用例通过；env_file 内容绕过见 R3-02。 |
| R2-06 | API/Web 凭据引用、Agent 选择、冻结与原生配置贯通；原离线反例关闭。真实调用未验。 |
| R2-07 | 默认文本下载脱敏、二进制默认拒绝已覆盖；操作员显式 raw-export 开关边界有说明。未新增确认缺陷。 |
| R2-08 | job 命中但 owner/run 冲突的原用例修复；project fallback 见 R3-03。 |
| R2-09 | 单条导入拒绝不再丢合法兄弟结果，失败单元补 indeterminate；原反例关闭。映射隔离分支见 R3-06。 |
| R2-10 | 禁止模型变化时阻断、允许时记录差异；原问题关闭。 |
| R2-11 | SDK 参数映射与拒绝已实现；公共模型参数丢失见 R3-04。 |
| R2-12 | DTO 校验提前，原非法 revision 和类型用例通过；原问题关闭。 |
| R2-13 | 原短尾反例通过，长尾和其他切点失败；见 R3-05。 |

## 复验交接

本机临时脚本：`/tmp/motte_m3r3_runtime_probe.py`（预检与假容器）、`/tmp/motte_m3r3_service_probe.py`（排队取消/错绑计划）；上一轮脚本 `/tmp/motte_m3_r2_service_actual.py`、`/tmp/motte-m3r2-storage-probe.py`、`/tmp/motte_m3_r2_compare.py` 已在当前提交重跑。服务新脚本命令为 `PYTHONPATH=. uv run python /tmp/motte_m3r3_service_probe.py`。这些临时文件不是长期仓库回归测试，修复时应固化对应反例。

修复顺序建议：先 R3-01/02/03 的有效配置与资源所有权，再 R3-06/07 的持久化边界，最后 R3-04/05 的公共配置与内容读取；随后运行 focused 回归与 `make check`。

另有一个尚未证实的兼容性核验项：固定 Harbor options 接受 reasoning_effort 并生成 `--effort`，但本轮未独立验证所固定 Claude CLI 版本实际支持该选项。此项没有列为确认缺陷，也不能以 schema 接受代替 CLI 兼容证据。真实模型调用继续单独授权，当前不自动触发或进入 M4。
