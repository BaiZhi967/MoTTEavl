# M3 实现 Review — Round 2（2026-09-20）

## 结论

**代码 review 仍不通过：7 项 P1、6 项 P2，共 13 项需要闭环。** 上一轮大部分直接反例已经得到实质修复；但公共导入、取消、retry、安全预检和真实 Agent 创建之间仍有组合路径缺口，不能将“26 项全部闭环”作为当前结论。

- 审查范围：`08d8f1e..046fa5fc6779c2ebdf2f7329f6c3361720349b7b`。
- 分支：`codex/m3-harbor-terminal-bench`；审查开始时工作区干净。
- 对照：[第一轮报告](M3-review-round1-2026-09-20.md)、[开发方修复记录](M3-review-round1-fixes-2026-09-20.md)、[M3 需求](../roadmap/M3-harbor-and-terminal-bench.md)、[实施计划](../superpowers/plans/2026-09-20-m3-kickoff.md)。
- 主审负责正式服务、评分与比较；三个独立只读子代理分别审查 Runtime、安全、公共接口与存储。本轮只新增本报告，不修改业务实现，不提交或推送。

## 验证证据与边界

重新执行 `make check`，退出码 0：Python **1436 passed / 43 skipped / 2 warnings**，Web **15 files / 216 tests passed**；lint、类型检查、构建、Compose/OpenAPI 等门禁通过。日志：`/tmp/motte-m3-round2-make-check.log`。

定向验证另覆盖公共 API/CLI（12 passed）、Web 相关测试（40 passed）、Memory/SQLite 存储与降级保护（28 passed、5 deselected）、Runtime 离线相关测试（56 passed）。这些定向测试与门禁有重叠，不能相加当成额外独立测试总数。

补充反例仅使用临时目录、临时 SQLite、合成 Runner outcome、现有脱敏产物 fixture、假 Docker client 和纯函数。**本轮没有真实 Provider 调用、真实 Docker Job/取消清理、真实 PG 并发或浏览器交互验收。** 开发方已有真实 PG/Docker 记录仍属于其提供的证据，本轮没有独立重跑；skip 不等于通过。

本机复现脚本（临时文件，不是仓库长期测试）：

- `/tmp/motte_m3_r2_service_actual.py`：正常执行、取消、通过实际 DurableExternalJobRunner + fixture adapter 的 retry。
- `/tmp/motte-m3r2-storage-probe.py`：跨 Run Trial 导入、单条非法结果导致整批中止。
- `/tmp/motte_m3r2_public_review.py`：API 凭据引用、原始工件下载、revision 类型、UTF-8 截断。
- `/tmp/motte-m3r2-runtime-review.py`：Compose 绕过、容器 owner 核验、配置映射；输出为同名 `.log`。
- `/tmp/motte_m3_r2_compare.py`：不允许模型变化时仍允许比较。另以 `claude-code@2.0.30`、同一凭据引用、两个不同 Anthropic 模型重复验证了相同结果，未执行模型。

下列定位均针对 `046fa5f`。复现表明现有测试的具体缺口，不表示整体测试失效；需要把补充反例纳入正式回归。

## P1

### M3-R2-01 — 取消在导入前终态化，已完成 Trial 仍丢失

- **定位：** `packages/sdk-python/motte_sdk/service.py:319`–321；`:334`；`_finish_unattempted` / `_ensure_trial_dispositions`。
- **复现：** 使用已有 fixture Runner 得到 4 条实际解析结果。在正式 `execute_external_job` 的 job_entry 返回前持久化取消请求。正常对照为 `completed/4 Trial/4 scores`；取消场景为 `cancelled/0 Trial/0 scores`，即使返回 outcome 明确包含 4 条完成结果。
- **根因：** `_honor_cancellation` 在导入 outcome 之前直接返回；Trial 计划又直到结果导入才创建，取消补处置时 repository 为空。若预先创建计划，提前写入的 synthetic 终态还会阻碍后续真实结果导入。
- **修复要求：** Run 创建/调度前持久化计划；外部取消协调“请求取消 → 中断 → 冻结并导入已确定结果 → 为未确定单元补处置 → 终态化”。不能为保存已完成证据而复活终态或自动重跑。以正式服务入口验证部分完成时取消、全部已采集但尚未导入时取消、跨进程取消请求。
- **关联：** 上轮 R01/R05 的公共组合路径仍未闭环；T06/T07、A07/A08。

### M3-R2-02 — retry 复制父 Run 的 Trial 身份，新 Run 没有自己的结果

- **定位：** `packages/sdk-python/motte_sdk/service.py:899`–912。
- **复现：** 先将一个含 4 Trial 的 Run 置为 failed，再执行公开 service.retry；新 Run 获得新 ID，但冻结 `task_manifest.trials[*].run_id` 仍全部为父 ID。通过实际 DurableExternalJobRunner 与 fixture adapter 执行子 Run，最终 `completed`，子 Run 的 Trial 数和 score 数均为 0。
- **根因：** 通用 retry 深拷贝整份 manifest，没有为 M3 重新派生 Run/Job/Trial 身份。导入仍指向父 Trial，旧证据可能被判 identical，也可能因新的 source_trial_id 变成冲突。
- **修复要求：** 保留任务内容、Profile 与实验条件，针对子 Run 重新生成 TrialPlan、原生 Job 身份及所有引用，并保留 parent_run_id。公共 retry 后跑完，断言父子 Trial ID 集合不相交、各自 run_id 正确、父证据不变、子分数完整。
- **关联：** 新发现的 M3 retry 集成缺口；G04/G05。

### M3-R2-03 — Trial 未核对当前冻结计划，可写入另一个 Run

- **定位：** `packages/sdk-python/motte_sdk/service.py:566`–568；`packages/sdk-python/motte_sdk/benchmark_plugins.py:299`–312。
- **复现：** 用正式 build_run_inputs 为 run-review 与 run-victim 生成相同任务的各自合法计划，并预存 victim 计划。review outcome 使用 victim 的 Trial ID、正确 task_key/repeat_index，不带 run_id（正常 payload 可以省略）。结果没有隔离行：review 显示 completed、0 scores、自身 Trial pending；victim Trial 被写成 succeeded。换成不存在的 ID，也会被静默忽略后假完成。
- **根因：** 有 payload.trial_id 时查不到当前 plan 仍继续；隔离 `plan is None` 的判断只存在于没有该 ID 的分支。importer 不处理 `unknown_trial`。仓库校验“payload 与目标 Trial 一致”不能保证该 Trial 属于当前执行 Run。
- **修复要求：** 先要求 Trial 属于当前冻结计划并核对 run/task/repeat，再导入；importer 再校验当前 Run 边界，对未知身份显式隔离/失败。验证另一个 Run 存在与不存在两类 ID，其他 Run 所有记录必须逐字不变。
- **边界：** 这是临时 SQLite 上的正式服务导入反例，不声称已通过 HTTP 发起跨 Run 攻击。上轮 R26 的直接存储反例已修，但服务集成未闭环。

### M3-R2-04 — Compose 间接挂载与插值路径仍绕过预检

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/compose.py:192`–194 及顶层 Compose 资源解析。
- **复现：** 任务中分别声明：命名 volume 的 `driver_opts` 为 `type=none/o=bind/device=/` 后挂入服务；顶层 `secrets.file=/etc/shadow` 后供服务使用；`${HOST_ROOT:-/}:/host` 的插值挂载。prepare 后公共 SDK preflight 均 `allowed=True`、空原因码。本轮没有执行这些 Compose。
- **根因：** 服务 volume 只检查当前字符串是否表现为 bind；命名 volume 直接 return，没有展开其定义。顶层 secrets/configs 文件源、Compose 插值后的真实路径也没有进入等价检查。
- **修复要求：** 对实际有效配置做完整、受控的资源源解析；无法证明安全的插值、外部文件、driver_opts 或资源形式拒绝，而非当作安全。加入这些反例并断言零任务启动。不要为了预检而运行任务脚本。
- **关联：** 上轮 R02 仅关闭直接 bind 的反例；T05、A13。
- **配置语义依据：** [Docker volumes / driver_opts](https://docs.docker.com/reference/compose-file/volumes/#driver_opts)、[Docker secrets 文件源](https://docs.docker.com/reference/compose-file/secrets/)。

### M3-R2-05 — Compose 环境透传形式可把 Runner 凭据带进任务

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/compose.py:267`–274。
- **复现：** `environment: [ANTHROPIC_API_KEY]` 或 `environment: {ANTHROPIC_API_KEY: null}` 均通过公共预检。检查固定 Harbor 0.23.0 源码确认 Compose 子进程使用包含 os.environ 的环境；平台 ProcessJobAdapter 也继承宿主环境。
- **影响：** 在真实 Claude Agent 所需的规范环境凭据存在时，Compose 的列表/空值透传语义会把该值带给任务服务。当前规则只检查 `$VAR` 插值，漏掉不含 `$` 的透传。其他宿主秘密变量只有实际存在于 Runner 环境时才受影响；本轮未读取真实秘密、未运行容器。
- **修复要求：** 同时检查变量名、列表透传和 mapping null 语义；最小化 Runner→Compose/任务的环境继承，区分可信 Agent 凭据与不可信任务环境。合成秘密测试应覆盖实际下发环境，不能只匹配 `${...}`。
- **关联：** 上轮 R02/R08 的安全边界延伸；G14、A13。
- **配置语义依据：** [Docker environment 无值透传](https://docs.docker.com/reference/compose-file/services/#environment)。

### M3-R2-06 — 真实 Agent API 创建没有必需凭据引用入口

- **定位：** `apps/api/app/main.py:1923`–1926、`:2021`–2028；preflight `:2179`。
- **复现：** 使用 published model 与 `claude-code@2.0.30`。不带 credentials 的创建返回 422 `HARBOR_AGENT_CREDENTIAL_REF_MISSING`；添加合法 `env:ANTHROPIC_API_KEY` 引用返回 422 `REQUEST_FIELD_UNKNOWN`。同参数 preflight 返回 ok=false、`AGENT_CREDENTIAL_REF_MISSING`。
- **根因：** DTO 禁止 credentials，构建 Profile 也未传入；新 Agent 能力却强制要求该引用。Web 仍只有 oracle 选项。CLI 支持引用、原生配置可加载不能证明 API/Web 已贯通。
- **修复要求：** 公共 DTO、preflight 和 UI 接通仅引用形式的凭据与首个真实 Agent；验证创建→冻结 Profile→原生配置，不调用真实模型。继续将真实模型执行列为待授权，不将本代码缺口只归因于 live 未授权。
- **关联：** 上轮 R10 未关闭。

### M3-R2-07 — 新原始字节下载路由绕过文本脱敏

- **定位：** `apps/api/app/main.py:2368`–2374；路由起点 `:2318`。
- **复现：** 同一合法 Trial 文本工件包含合成 `sk-review-sentinel-0123456789abcdef`。普通内容接口返回脱敏文本；加 `/bytes` 返回 200 和原始哨兵，响应头 `X-Motte-Artifact-Redacted:false`。
- **根因/影响：** 工件归属和 hash 校验不等于秘密展示保护；原始字节接口适用于文本日志，完整绕过本轮增加的脱敏边界。文档声明“raw 不脱敏”和响应头标注不能防止泄露。
- **修复要求：** 普通内容查看/下载遵守同一秘密保护边界。若保留原始证据导出，需与普通读取明确隔离并提供受控访问，不能仅依赖调用者知道应使用另一路由；原始 Artifact 身份与 hash 不因展示脱敏被改写。对文本、二进制元数据及错误内容使用合成秘密回归。
- **关联：** 上轮 R08 的 detail 反例已修，新 R19 下载路径重新引入泄露。本轮不扩展审查整个平台认证体系。

## P2

### M3-R2-08 — 容器核验忽略 owner token 和 Run，可能停止错误资源

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/containers.py:123`–127。
- **复现：** 假 Docker client 返回一个 motte.job 匹配、但 motte.run 与 motte.owner 均不同的容器。stop_owned(remove=True) 仍调用 stop/remove 并报告 clean。
- **根因：** 构造器保存的 run_id/owner_label 没有参与 `_matches`；只要 job 标签或 compose project 匹配即认可，即使其他所有权证据明确冲突。
- **修复要求：** 操作前核对完整持久化所有权证据；冲突时拒绝清理并记录 unknown/needs_review 或相应残留状态，不能用 project fallback 覆盖明确 owner 不一致。扩展 fake client 与真实受控 Docker 的诱饵测试。本轮没有操作真实容器。
- **关联：** 上轮 R04 已接通 Docker 操作，但所有权核验未完整闭环。

### M3-R2-09 — 一个非法 Trial 中止整批导入，合法兄弟结果丢失

- **定位：** `packages/sdk-python/motte_sdk/benchmark_plugins.py:302`–306；`service.py:337`–346。
- **复现：** 2 Task × 2 repeat 均有成功结果，只让排序第二条 payload 带错 run_id。最终 failed/TRIAL_IMPORT_INVALID（拒绝本身正确），但只有 1 score，Trial 状态为 succeeded/pending/pending/pending；第三、第四条合法完成结果没有导入。
- **根因：** put_result 抛错跳出整个循环，服务只捕获整批异常。Task 派生行却仍取自全部 outcome，而非已接受记录。
- **修复要求：** 逐条隔离错误，继续保存合法结果；返回完整错误清单，为被拒绝单元保留明确处置，派生视图与已落库事实一致。保持 Run failed，不因部分成功掩盖冲突。
- **关联：** 上轮 R01/R26 的集成回归；G10。

### M3-R2-10 — 比较政策禁止模型变化时，TB 模型差异仍被忽略

- **定位：** `packages/evaluators/motte_eval/comparison.py:96`–105。
- **复现：** 两份正式 build_run_inputs manifest，相同 Claude Agent/凭据/任务/预算，模型分别为 `claude-sonnet-4-5` 与 `claude-opus-4-1`；ComparisonPolicy.allowed_factors 为空。返回 eligible=true、quality=true，只有 COST_UNKNOWN。
- **根因：** 特殊 model 分支只比较 manifest 顶层 model；TB 实际模型在 external_benchmark.profile.model，且未进入后续字段比较。
- **修复要求：** 按 suite 的实际冻结模型身份取值；未允许 model 变化时阻断，允许时记录差异。不能仅测试 allowed_factors 含 model 的成功样例。
- **关联：** 上轮 R09 的非模型条件反例已修，模型政策仍有遗漏。

### M3-R2-11 — Agent 接受的部分模型/工具/预算配置没有执行映射

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/config.py:348`–349；resolve_agent_profile 的模型/tools/limits 接受逻辑。
- **复现：** 正式 SDK 为 Claude Agent 构造带 reasoning_level、model.parameters、tools、limits 的 Profile，与去掉这些设置的 Profile 比较，生成的原生 job 执行配置相同。当前 kwargs 只下发 version，另有 timeout 的独立映射。
- **影响：** 配置被接受、保存并影响指纹，但实际 Agent 使用默认值；预算或工具策略不能靠 snapshot 声称生效。
- **修复要求：** 逐字段对接固定 Harbor Agent 支持的执行参数；不支持的非空配置在创建前拒绝。用原生 options/实际下发参数验证，不只断言冻结 Profile 中存在字段。真实模型调用仍不需要为此自动触发。
- **关联：** 上轮 R10/T03 的配置保真仍有缺口。

### M3-R2-12 — dataset_revision 类型校验晚于数据库访问，仍可返回 500

- **定位：** `apps/api/app/main.py:2226`–2231。
- **复现：** 已准备数据集，创建请求分别传 `dataset_revision:{"bad":"type"}` 和 `dataset_revision:["bad"]`，TestClient 均得到 500。
- **根因：** 数据集查询先于完整 DTO 校验，revision 字段也没有被 Profile 校验器检查。
- **修复要求：** 在任何 repository 查询前验证完整请求字段类型，返回结构化 422；断言非法输入不会创建 Run/Job。
- **关联：** 上轮 R20 原三个例子已修，同类公共校验遗漏仍存在。

### M3-R2-13 — UTF-8 截断切开字符，合法长日志整体变成“二进制”

- **定位：** `packages/sdk-python/motte_sdk/terminalbench.py:693`–703。
- **复现：** 有正确 SHA 的合法 UTF-8 字节 `("a" * 65535 + "中" + "z").encode()`，read_artifact_text 返回 verified=true、truncated=true、encoding=binary、text=None。
- **根因：** 先截字节预算再严格 decode，预算尾部不完整的多字节字符被当作整个文件不是 UTF-8。API/CLI/Web 共用该 helper。
- **修复要求：** 用增量解码或回退到完整字符边界，保留截断标记；区分真正非法 UTF-8 与预算边界半个字符。覆盖中文和 emoji。
- **关联：** R19 新内容读取路径的回归。

## 上轮反馈复核结果

| 上轮编号 | 本轮判断 |
|---|---|
| R01 | 正常 4 Trial 导入、评分和 rescore 原反例通过；取消、跨 Run 身份、部分错误隔离仍见 R2-01/03/09。 |
| R02 | 直接危险 bind/privileged 等已有拒绝；间接资源及环境透传仍见 R2-04/05。 |
| R03 | 冻结内容清单、副本、执行前校验已接入；离线回归通过。 |
| R04 | Docker 资源操作与可观察状态已接入；owner 冲突仍见 R2-08。本轮未复验真实容器清理。 |
| R05 | Job 定位提前持久化、部分输出可采集的原问题已修；服务取消仍提前丢掉采集结果，见 R2-01。 |
| R06 | Verifier 错误优先于残留 reward 的反例通过。 |
| R07 | Job deadline 与 build/setup 期限已有映射和离线阻塞测试；本轮未重跑真实 Job 超时。 |
| R08 | Trial detail 脱敏已修；原始下载绕过见 R2-07。 |
| R09 | 重复数、预算、资源与环境变化的原反例通过；模型政策见 R2-10。 |
| R10 | CLI 模型校验、真实 Agent 原生配置有实现；API 凭据入口与参数执行缺口见 R2-06/11。 |
| R11/R12/R13 | 二进制证据持久化、外层安全读取、同 basename 的完整路径归属已有修复及离线回归。 |
| R14/R15 | first-trial 不跳过无效首项；未知费用不生成完整零成本比率，原反例通过。 |
| R16/R17/R18 | Web timeout DTO、CLI 冻结计划、共享 Gate 的 Trial 指标已接通，原定向用例通过。 |
| R19 | 真实文本工件读取、归属/hash 校验、UI 下钻已有实现；下载安全与 UTF-8 回归见 R2-07/13。 |
| R20/R21 | 原列举非法请求与 Run 成本范围已修；revision 类型遗漏见 R2-12。 |
| R22/R23 | PG 首写改用 ON CONFLICT，降级新增数据保护；本轮 PG 仅静态复核，Memory/SQLite 相关 guard 测试通过。 |
| R24/R25/R26 | Trial identity 不可变、出口深拷贝、目标与 payload 一致性直接反例通过；R26 的调用方 Run 边界见 R2-03/09。 |

## 下一轮验收重点

1. 先修 R2-01/02/03/09：从公共创建→执行→取消/retry→导入→ScoreSet 验证结果归属；不要以 supervisor 单测代替 RunService 整链。
2. 修 R2-04/05/07/08：检查有效 Compose、环境下发、公开证据与资源所有权；未知或冲突的边界应拒绝执行/清理，而非报告安全。
3. 修 R2-06/10/11/12/13：贯通公共真实 Agent 配置、比较政策、输入类型和真实 UTF-8 内容消费。
4. 每项保留失败反例与修复后同反例通过的记录，最后运行 `make check`。真实 PG、真实 Docker 取消清理、真实 Agent 小批次分层记录，不能将离线通过与 live 验收混写。

代码反馈尚未闭环，当前不建议进入 M4。
