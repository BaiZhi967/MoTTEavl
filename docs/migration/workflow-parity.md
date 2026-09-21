# 旧 Scenario DSL 到 Workflow 的语义对照（M5-T01）

状态：转换器已实现并有诊断测试；**旧项目源码在本环境不可用**，因此字段与
语义按已文档化的 DSL 实现，源码级复核仍是未完成的外部条件（见第 5 节）。

## 1. 只读来源

| 项 | 值 |
|---|---|
| 仓库 | BaiZhi967/llm_agent__evaluation_platform |
| commit | b661bcdf83e1c3dfb8d6062ee78817d249e86a4c |
| 路径 | workers/scenario-runner/src/scenario_runner/engine.py |
| 交接记录的大小 / SHA256 | 22,561 字节 / 11093949c0e9dee75c52217ac6d1ca147249b64670f78037884e503fd8880a94 |
| 本机可用性 | 不存在（未随仓库分发，本环境无网络获取许可） |
| 本阶段是否复制代码/资源 | 否 |

转换器实现于 packages/scenario-runtime/motte_scenario/conversion.py。它只
读取结构化数据，不导入旧模块、不执行旧代码；表达式只经 ast 解析（只读），
永不进入 eval。

## 2. 字段映射矩阵

| 旧字段 | 新字段 | 语义 | 说明 |
|---|---|---|---|
| given.fixtures | fixture_draft + fixture_refs | preserved | 初始数据进入 Fixture 草稿，由 T02 发布为不可变 FixtureVersion |
| given.initial_state | fixture_draft.initial_state | preserved | 同上 |
| given.conversation_history | fixture_draft.conversation_history | preserved | 固定输入历史属于数据，不属于步骤 |
| steps[].when.send_message | steps[].kind=send_message | preserved | |
| steps[].when.message | steps[].kind=send_message | preserved | 兼容别名 |
| steps[].when.invoke_tool | steps[].kind=invoke_fixture_tool | renamed | 工具名/参数/模式逐字段保留 |
| steps[].expect | steps[].assertions | preserved-as-assertion | **断言**语义，不转成 when |
| steps[].checkpoint | steps[].kind=checkpoint | preserved | label 保留 |
| steps[].when.branch | steps[].kind=branch | preserved-order | condition 映射为 **when**（动作判定） |
| steps[].when.loop | steps[].kind=loop | bounded | max_iterations 必填；缺失即拒绝 |
| final_assertions | completion_assertions | renamed | |
| limits | limits | preserved | 三个字段都必须显式声明 |
| setup | — | refused | shell=True 语义不迁移 |
| cleanup | — | refused | 同上 |
| user_simulator | — | refused | 模型用户模拟器默认拒绝；确定性脚本请写成 send_message 步骤 |
| checker | — | refused | 不可表达的隐藏 checker 不迁移 |
| on_error / hooks | — | refused | 未文档化的控制面不迁移 |

未知顶层字段产生 LEGACY_FIELD_UNKNOWN 警告（不阻断发布），并逐条列出路径，
不静默丢弃。

## 3. 表达式转换

旧引擎用 eval(expression) 求值条件。新转换只接受下列语法，其余全部产生诊断：

| 旧表达式 | 新条件 |
|---|---|
| state.order.status == 'cancelled' | {op: eq, path: state.order.status, value: cancelled} |
| context.retry != 2 | {op: ne, path: state.retry, value: 2}（context → state） |
| state.status in ['a', 'b'] | {op: in, path: state.status, value: [a, b]} |
| state.order is not None | {op: exists, path: state.order} |
| a and not b | {op: and/or/not, conditions: [...]} |

根标识符映射：state/context/session/result → state；tool_results/tools →
tool_results；steps/step_results → step_results。未列出的根（例如
secrets、env、os）产生 LEGACY_EXPRESSION_ROOT_FORBIDDEN。

明确拒绝（LEGACY_EXPRESSION_UNSUPPORTED）：函数调用、下标、算术、
f-string、lambda、属性赋值、以及任何 ast 白名单之外的节点。没有"近似"
转换。

## 4. 诊断与发布门

诊断分级：

- error：转换不可发布（LEGACY_FIELD_REFUSED / LEGACY_EXPRESSION_UNSUPPORTED /
  LEGACY_EXPRESSION_ROOT_FORBIDDEN / LEGACY_LOOP_UNBOUNDED /
  LEGACY_STEP_ID_MISSING / LEGACY_STEP_ID_DUPLICATE /
  LEGACY_STEP_ACTION_MISSING / LEGACY_ACTION_UNSUPPORTED /
  LEGACY_FIELD_MALFORMED / LEGACY_LIMITS_MISSING / LEGACY_STEPS_EMPTY /
  LEGACY_CONDITION_UNSUPPORTED / LEGACY_PUBLISHED_AT_REQUIRED /
  LEGACY_CONTRACT_INVALID）。
- warning：可继续（LEGACY_FIELD_UNKNOWN）。

ConversionResult.publishable 只在没有 error 时为真；
to_publishable_workflow() 在有 error 时抛 ConversionIncompleteError 并列出
全部阻断码。发布时间不由转换器发明：调用方显式传入 published_at，内容
hash 在发布时计算并固定。

runs_executed 恒为 0：转换不执行任何步骤、工具或模型调用。

## 5. 未完成的外部条件

以下内容**没有**可核验证据，不得据此宣称源码级语义对齐：

- 旧仓库该固定 commit 的源码文件在本环境不可用，无法逐字段核对 v1 引擎的
  真实字段名、默认值与边界；当前转换器覆盖的是计划与路线图文档中明确记录
  的 DSL 语义。
- 因此 M5-T01 的“旧 DSL 不支持字段必须出现在诊断中”按文档化语义验证通过，
  “与旧引擎源码逐字段一致”仍为 not_run，需要在能访问固定 commit 的环境
  重新核对后更新本节与 docs/verification/M5.md。
