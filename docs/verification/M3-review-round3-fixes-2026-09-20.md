# M3 第三轮反馈修复与复核（2026-09-20）

对应 [第三轮 review](M3-review-round3-2026-09-20.md)，修复基线 `33767bf`。本轮由两个子代理分别修复运行环境和生命周期，主代理修复公共入口/UTF-8 并完成集成复核。七项反馈在代码与离线回归层面关闭；本记录不代替真实 Provider、Harbor Agent 或 PostgreSQL 验收。

| 反馈 | 修复与独立复核结论 | 持久回归 |
|---|---|---|
| R3-01 / P1 | 宿主来源中的 `$` 表达式一律拒绝，不再用默认值冒充有效环境；volume/build context/dockerfile/env_file/resource file 均检查 | `tests/benchmarks/test_harbor_task_compose_policy.py`：HOME、HOST_ROOT、嵌套/有默认表达式及所有来源字段 |
| R3-02 / P1 | env_file 用 TrustedDir 有限读取，仅支持单行无动态语义的字面 KEY=value；拒绝插值、透传、引号、续行、缺失/不安全文件和非法 UTF-8 | 同文件：合成凭据依赖、文件和内容边界；公共 prepare→preflight 无任务启动 |
| R3-03 / P2 | 对已有 run/owner/job 标签独立检查冲突，再做正向 job/project 判定；动作前重验 | `tests/integration/test_harbor_container_ownership.py`：project-only 正常兼容、显式冲突不 stop/remove、读取阶段标签变化 |
| R3-04 / P2 | API/CLI 共用 published_model_config，把非空 parameters 与默认 reasoning_level 保留到 map-or-reject；null 默认不当请求，无法映射则具名拒绝且不入队 | `tests/api/test_terminalbench_round2_fixes.py`：真实发布资源→API 422 / CLI 非零；原空参数创建仍成功 |
| R3-05 / P2 | UTF-8 解码前一次限定输入预算，增量 final=False 仅舍弃末尾不完整字符；非法输入仍 binary | `tests/sdk/test_terminalbench_content_read.py`：中文/emoji 四种切点、短尾/5000 字节长尾、非法字节；返回字节数不超预算 |
| R3-06 / P2 | 创建即持久化计划；终态补齐缺失单元，取消=cancelled、启动前失败=not_attempted、运行后不确定=indeterminate；保留确定结果 | `tests/integration/test_harbor_round3_lifecycle.py`：queued cancel、启动/采集失败、未知结果隔离、profile_stale；全部四个计划单元和评分仍存在 |
| R3-07 / P2 | create/dispatch/import 在修改前全批次校验计划 Run 归属、重复身份和存储计划一致性；后部错误不会提前写前部计划 | 同 lifecycle 测试与 `tests/storage/test_trial_store.py`：跨 Run/已存储冲突、零 launch、受害 Run 字节级记录不变、无部分写入 |

## 回归与 review

- Runtime 新反例先复现 19 个失败；后续定向测试 81 passed / 1 deselected。
- Public 新反例先复现 11 个失败；修复后 API/CLI/SDK/native mapping 定向 81 passed。
- Lifecycle 新反例先复现 10 个失败，另有 profile_stale 红绿回归；最终定向 62 passed / 4 skipped。
- 全量初次发现的 custom-suite 提前解析回归已修复；API 跨 Run 读取测试改为给第二个 Run 单独冻结计划，不能继续使用其他 Run 的 manifest；Compose 旧危险默认值断言更新为新的 unresolved 原因码，仍断言拒绝。
- 主代理读完整修改与关键调用链，逐项核对行为、身份和副作用。生命周期代理另行只读复核公共配置与 UTF-8；未发现新的阻断缺陷。

最终 `make check` **exit 0**：

- ruff 通过；mypy 25 个契约文件通过；compileall 通过。
- Python：**1574 passed, 45 skipped, 1 deselected, 2 warnings**，59.43 秒。
- Web：**15 文件 / 223 tests passed**；Web production build 通过；Pi bridge 与其余 workspace 测试通过。
- Docker Compose config 静态校验通过；OpenAPI 一致性通过。
- `git diff --check` 通过。

主门禁日志：`/tmp/motte-m3-r3-final-check.log`（本机临时日志，长期证据为仓库回归与本记录）。两个 Python warning 为既有 Starlette/httpx 与 anyio deprecated API 提示。45 个 skip 保留未验状态，不计通过；真实容器用例新增 live 标记且不在 collection 阶段探测 Docker，默认 `-m 'not live'` 排除。

## 兼容性与证据边界

- Compose 安全策略更保守：即使默认路径看似安全，宿主来源插值仍拒绝；env_file 的引号、续行、插值及缺失 optional 文件均不支持。请转换成任务目录内可受控读取的字面配置。
- 带非空且 Harbor 无法表达的 ModelProfile 参数，现在拒绝创建，而不再悄悄丢弃；模型选择可追溯与实际执行要求优先。
- SDK 调用者必须为目标 Run 重新冻结计划；跨 Run 重用原 manifest 现在明确失败。retry 沿用已实现的子 Run 重新冻结路径。
- 本轮没有真实 Provider/付费模型、容器执行或 PG E2E。此前尚未独立核验的 Claude CLI 固定版本与 reasoning flag 兼容性没有被本轮测试消除，不据此宣称 live 全面验收。
- 本次提交/合并关闭的是上述七项代码反馈；允许开始 M4 开发规划，不把其他阶段或外部环境验收自动标成完成。
