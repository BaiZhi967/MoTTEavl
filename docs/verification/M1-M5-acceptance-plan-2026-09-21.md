# M1–M5 产品级验收测试清单（2026-09-21）

对象：`E:/Dev/MoTTEavl`，分支 `codex/m5-scenarios-skills-judges`，HEAD `63b8f2f`。
方式：真实进程（API 8000 / Web 5173 / Worker），CLI + Web 两条入口，真实模型 `deepseek-v4.1-flash`（provider `6a`）。
本清单是**执行清单**：每条有编号、执行命令/页面、期望判据与判定结果位。执行记录见
`docs/verification/M1-M5-acceptance-report-2026-09-21.md`。

## 0. 判定口径（先定规则，再看结果）

| 判定 | 含义 |
|---|---|
| PASS | 按清单实际执行，观察到的结果与期望一致；有可复现命令与输出 |
| FAIL | 实际执行，结果与期望不一致（产品缺陷） |
| REFUSED-OK | 期望就是「具名拒绝」；实际给出具名错误码且不静默降级 |
| not_run | 因环境或授权缺失**未能执行**，必须写出缺失项；不得记为 PASS |
| N/A | 契约明确不支持（例如 pairwise 公共入口），且已在文档登记 |

三条自我约束：
1. **零费用路径**先跑，付费调用后跑，且每次付费前后记录 invocation 计数与费用，证明「零模型调用」不是口号。
2. **未知保持未知**：任何缺证据/缺授权一律记 not_run 或 unknown，不用推测填空。
3. **口径分离**：既有测试（`make check`、pytest）结论与我本次手工验收结论分列，不互相冒充。

## 1. 环境基准（执行前实测）

| 项 | 实测 |
|---|---|
| API | http://127.0.0.1:8000 `GET /health` → 200 |
| Web | http://127.0.0.1:5173 → 200（Vite dev server） |
| Worker | 进程 `python -m apps.worker.motte_worker` 在运行 |
| Provider | `6a` → https://6a.g-bits.com/v1（openai_compatible），凭据 profile `6a` |
| 已发布模型 | `deepseek-v4.1-flash`（ctx 1e6）、`gpt-5.6-luna`（ctx 256k） |
| scenario target | 仅 `builtin-agent`（multi_turn、tool_modes real/mock/replay/deny、skill_injection） |
| runtime 矩阵 | `pi-agent@1`（installed、protocol_ready，**execution_ready=false**）、`claude-cli@1`、`codex-cli@1`、`codex-app-server@2` |
| 已有数据 | 13 个 Run；direct-llm 数据集 2 个；**workflows/skills 均为 0** |

## 2. 分层与预算

| 层 | 覆盖 | 费用 |
|---|---|---|
| L0 环境与自检 | E01–E05 | 0 |
| L1 零费用入口（只读/预检/具名拒绝） | A01, B01–B02, C01–C03, D01–D03, E01–E03, E08, E11, F01 | 0 |
| L2 真实模型小规模 | A02–A06, B04–B05, E05, E09, E12–E13 | 少量（flash，逐次记账） |
| L3 外部依赖（Docker / C-Eval 数据 / 个人 CLI 配额） | B06, C04–C05, D05 | 视授权 |

## 3. 清单

### M1 原生 Agent 与通用评分闭环

| ID | 被测目标 | 执行方式 | 期望判据 |
|---|---|---|---|
| A01 | 文件任务数据集入口 | `motte agent-tasks import/list`、`POST /api/v1/agent-tasks/import` | 自编数据集导入成功，字段（cases/指纹）可见 |
| A02 | 真实模型驱动 Builtin Agent 完成文件任务 | `POST /api/v1/agent-tasks/runs` + Worker | Run completed；输出文件存在且内容断言通过 |
| A03 | 工具失败后回灌 observation 并恢复 | 同 A02 的第二个 case | 失败步骤留在证据里，恢复后任务达成 |
| A04 | 预算与终止 | 故意设置极小 max_steps | 明确停止原因；不谎报成功；不伪装硬预算 |
| A05 | 两个已发布模型对比 | `gpt-5.6-luna` 同一任务 | 两次 Run 都进入真实工具循环，执行模式进快照 |
| A06 | 失败发生在哪一步可定位 | 下钻 API + Web 结果页 | 逐步事件/工具调用可见，指向具体 step |
| A07 | 无凭据、无 gold 泄漏 | 读 invocation/event/artifact 内容 | 不含 gold/隐藏断言/API key |

### M2 C-Eval 与正式 LLM Benchmark

| ID | 被测目标 | 执行方式 | 期望判据 |
|---|---|---|---|
| B01 | direct-llm 工作区 | `motte direct-llm builtins/list`、Web /direct-llm | 数据集、分母、覆盖可核对 |
| B02 | GSM8K 工作区 | `GET /api/v1/benchmarks/gsm8k` | 数据集版本/revision/license 可见 |
| B03 | 能力不足不冒充可运行 | `POST /api/v1/benchmarks/direct-llm/dry-run` | 预检给出选择数/上界/价格表版本，不发起调用 |
| B04 | 小规模真实运行 | direct-llm 1–2 case | 逐样本输出、指标、失败说明、分母一致 |
| B05 | GSM8K 小规模真实运行 | smoke profile | 总分与分母/覆盖率可核对 |
| B06 | C-Eval 端到端 | `motte ceval prepare/preflight/run` | 无本地 C-Eval 数据 → 具名阻断（不伪造成绩） |
| B07 | 可比性提示 | 异集/异版本比较 | 被阻断或标注不可比 |
| B08 | Web 页面能力原因 | /ceval 等页面 | 不可执行控件显示具体原因 |

### M3 Harbor / Terminal-Bench

| ID | 被测目标 | 执行方式 | 期望判据 |
|---|---|---|---|
| C01 | 未准备数据集时具名拒绝 | `motte terminal-bench tasks/preflight` | `DATASET_UNPREPARED`，不返回空成功 |
| C02 | 受控任务根准备 | `motte terminal-bench prepare` | 若无可准备来源 → 具名拒绝并说明 |
| C03 | 容器依赖探测 | Docker 可用性 | 未安装 → M3 live 记 not_run，不冒充通过 |
| C04 | Task/Trial 契约 | `GET /runs/{id}/tasks`、`/trials` | 无 Run 情况下返回明确空/404，不伪造 Trial |
| C05 | Web 页面 | /terminal-bench | 能力不足原因可见 |

### M4 Pi 与 CLI Harness

| ID | 被测目标 | 执行方式 | 期望判据 |
|---|---|---|---|
| D01 | 分层就绪状态 | `motte runtime` 目录/就绪 | pi 已安装 + protocol_ready，`execution_ready=false` 并给出理由 |
| D02 | 规范版本发布 | `motte runtime publish` | 幂等；同内容一致、异内容冲突 |
| D03 | 未接通能力明确拒绝 | 交互式命令 `GET /runs/{id}/commands` | 明确拒绝/空，不假装已接通 |
| D04 | Pi 真实执行 | `POST /runs` 带 pi-agent | 因 execution_ready=false → 具名拒绝，不静默改选 backend |
| D05 | Claude/Codex CLI 真实小任务 | 需操作者授权（消耗个人配额） | 未获授权 → not_run |
| D06 | Inspect 只读导入 | `motte inspect-import` | 只读导入日志，不执行其中内容 |
| D07 | Web 页面 | /harnesses、/runtimes | 就绪状态与阻断原因一致 |

### M5 场景、Skill、Judge

| ID | 被测目标 | 执行方式 | 期望判据 |
|---|---|---|---|
| E01 | 发布 WorkflowVersion + FixtureVersion | `motte workflow publish` | 同内容幂等、异内容 409、draft 不可发布 |
| E02 | 纯预检 | `motte scenario validate` | `publishable/executed=false`，零调用 |
| E03 | 旧 DSL 只读转换 | `motte workflow convert-legacy` | 只出诊断与映射，不发布不执行 |
| E04 | 公共入口跑通场景 | `motte scenario run` + Worker | Run completed，冻结 Observation 生成 |
| E05 | 多指标断言 | state-equals / no-side-effect / goal-achieved | 每指标带版本，缺证据不判通过 |
| E06 | 工具模式与权限 | deny 模式 / 越权工具 | 具名拒绝，不落地 |
| E07 | 副作用与失败定位 | 制造断言失败 | 失败可定位到 step；不伪造 pass |
| E08 | Skill 导入与静态校验 | `POST /api/v1/skills`、`/validate` | kind=instruction 不谎称已执行 |
| E09 | 可执行 Skill fixture | entrypoint 受控运行 | 输出/权限/越界失败各自具名 |
| E10 | 三臂对照 | no-skill / v1 / v2 | 三条普通 Run + 配对报告；成本未知保持 unknown |
| E11 | Judge 预检 | `motte judge preflight` | 零费用、零作业、max_calls=len(plans) |
| E12 | Judge 真实作业 | `motte judge submit` + Worker | 原子发布 ScoreSet+pass+receipt+current |
| E13 | 重评分与历史 | 再次 submit / rescore | 追加新 pass，旧基线仍指向旧 pass |
| E14 | 零模型调用证明 | GET/history/cancel 前后 invocation 计数 | 计数不变 |
| E15 | 校准资格 | 无人工资料 | `experimental`、`gate_eligible=false` 且给原因 |
| E16 | 多指标 Gate | `POST /api/v1/gates` | 多指标不误算 attempted |
| E17 | Web 页面 | /scenario、/skill、/judges | 展示真实 Run/pass，不猜测 |

### 横向（F）

| ID | 被测目标 | 期望判据 |
|---|---|---|
| F01 | 冻结不可变 | 资源改名/升级后旧 Run 仍用原快照 |
| F02 | 秘密不入库 | job/pass/invocation/event/report/日志无明文密钥 |
| F03 | 成本如实 | 未知保持 unknown，不补 0 |
| F04 | 取消与清理 | 取消后 Run 停 cancelled，受控进程确认停止 |

## 4. 自编测试数据

| 名称 | 用途 | 说明 |
|---|---|---|
| `acc-file-task@1` | M1 文件整理任务 | 3 个 case：正常创建、工具失败后恢复、越权/超预算 |
| `acc-order-cancel` Workflow | M5 主流程 | 询问必要信息 → 查询订单 → 取消 → 最终状态断言 |
| `acc-order-fixture` | M5 Fixture | 订单初始状态 + 允许工具 + 禁止副作用 |
| `acc-skill-brief@1` | M5 纯指令 Skill | 声明权限，验证渲染进真实请求 |
| `acc-skill-audit@1` | M5 可执行 Skill | entrypoint + output_schema + 越权用例 |

所有自编实体统一带 `acc-` 前缀，便于报告里给出清理方式。

## 5. 执行顺序

1. L0 环境自检 → 2. M1 → 3. M5（因需自建资源，先离线建好）→ 4. M2 → 5. M3/M4 阻断探测
→ 6. Web 走查 → 7. 零调用/费用/秘密边界复核 → 8. 出报告。

每个 FAIL/not_run 都要在报告里带：命令、原始输出、缺失项、对里程碑退出门的影响。
