# Scenario Workflow 协议（M5-T01）

状态：M5-T01 已实现并有测试证据；T02–T05 扩展其执行与装配边界。

本文件是 Workflow DSL 的**唯一事实源**。契约在
packages/contracts/motte_contracts/workflow.py，纯解析器在
packages/scenario-runtime/motte_scenario/compiler.py，受限条件在
packages/scenario-runtime/motte_scenario/conditions.py。

## 1. 身份分层

ScenarioVersion 负责 dataset / model / runtime / skill / environment /
evaluator / 预算引用；WorkflowVersion 负责**如何逐步驱动**目标。二者不是
同一身份：

- Run 的 execution backend 是 scenario 形态（T05 注册），Workflow 不产生
  子 Run，也不产生伪 Trial；
- 外层 backend 身份（例如 scenario@1）与内层 target runtime 身份（例如
  pi-agent@1）分别冻结在 manifest 的不同字段；
- Workflow 步骤复用既有 RunDispatcher、CaseAttempt、InvocationRecord、
  Artifact、FrozenObservation、ScoringPass。

存储主键为 (workflow_id, version)，语义与 runtime_versions 相同：发布即
不可变，同版本同内容幂等，同版本异内容冲突，不可删除。Alembic 版本
0011_workflow_resources。

## 2. 完整有效样例

```yaml
workflow_id: order-cancel-confirmed
version: "1"
schema_version: 1
description: 取消订单前必须取得用户确认
published_at: "2026-09-21T00:00:00Z"
fixture_refs:
  - fixture_id: order-state
    version: 1
    kind: json
    content_hash: "sha256:..."
    allowed_tools: [orders.get, orders.cancel]
target_requirements:
  multi_turn: true
  min_turns: 2
  required_tools: [orders.get, orders.cancel]
  tool_modes: [mock]
  interrupt: true
  evidence: [events, invocations, artifacts]
steps:
  - step_id: request
    kind: send_message
    message: "请取消订单 order-1"
    timeout_sec: 20
  - step_id: before-confirm
    kind: checkpoint
    assertions:
      - {op: eq, path: state.order.status, value: active}
      - {op: eq, path: state.order.cancellation_count, value: 0}
  - step_id: confirm
    kind: send_message
    message: "我确认取消"
  - step_id: cancellation-count
    kind: invoke_fixture_tool
    tool: orders.get
    arguments: {order_id: order-1}
    tool_mode: mock
    assertions:
      - {op: eq, path: tool_results.orders.get.cancellation_count, value: 1}
  - step_id: final-check
    kind: checkpoint
    label: final
    assertions:
      - {op: eq, path: state.order.status, value: cancelled}
      - {op: eq, path: state.order.cancellation_count, value: 1}
      - {op: not, conditions: [{op: eq, path: state.order.refunded, value: true}]}
completion_assertions:
  - {op: eq, path: step_results.final-check.status, value: cancelled}
limits: {max_total_steps: 20, max_turns: 4, wall_time_sec: 30}
failure_policy: stop_case
lifecycle: published
content_hash: "sha256:..."
```

内容 hash 由 workflow_content_hash 计算，覆盖除 lifecycle / published_at /
content_hash 以外的一切字段：草稿编辑不改变 hash，内容改变一定改变 hash；
发布前必须自洽，否则资源仓库拒绝入库。

## 3. 七类步骤

| kind | 语义 | 关键约束 |
|---|---|---|
| send_message | 向 Target 发一轮业务消息 | message 非空；正常 final answer 只结束本 turn |
| invoke_fixture_tool | 平台侧直接调用受控 fixture 工具 | tool_mode ∈ real/mock/replay/deny |
| assert | 紧随动作的断言，无副作用 | 至少一条 assertion |
| checkpoint | 冻结当前状态快照 | 审计点，不自动授予续跑能力 |
| branch | when 是**动作判定**，选择执行子树 | then/else 至少一棵非空 |
| loop | 有界循环 | max_iterations 必填且 ≤ 64；until 可选 |
| trigger_fixture_event | 注入确定性事件 | event 名合法 |

**when 与 expect 不互相误译**：when 保留动作语义（branch 的判定条件），
expect/assertions 是紧随动作的断言。branch.when 与 step.assertions 在契约里
是两个不同字段。

结构上限：嵌套深度 ≤ 4（MAX_STEP_NESTING_DEPTH），展开节点 ≤ 256
（MAX_WORKFLOW_STEPS），全局 max_total_steps ≤ 2048、max_turns ≤ 128、
wall_time_sec ≤ 3600 秒。所有嵌套 step_id 全局唯一；input_ref 只能指向**先
声明**的步骤。

## 4. 受限条件

```json
{"op": "eq", "path": "state.order.status", "value": "cancelled"}
{"op": "in", "path": "state.order.status", "value": ["active", "cancelled"]}
{"op": "exists", "path": "state.order.refunded"}
{"op": "and", "conditions": [ ...至少两个... ]}
{"op": "not", "conditions": [ ...恰好一个... ]}
```

- 路径根只允许 state / tool_results / step_results；其他根在编译期拒绝。
- 路径段只允许字母开头的普通标识符：dunder、下标、调用语法在契约层就被
  拒绝；编译期再做一次根白名单校验（纵深防御）。
- 深度 ≤ 8，节点 ≤ 64；嵌套过深或节点过多在编译期拒绝。
- **运行期缺失字段是明确错误**（CONDITION_PATH_MISSING），不是 false；
  唯一把缺失当作否定的是 exists。
- bool 不冒充 int：{op: eq, value: 1} 对 True 不成立。
- 不执行 eval / import / 属性调用 / shell / 算术 / f-string。

## 5. Target 能力要求

target_requirements 声明 multi_turn / min_turns / required_tools /
tool_modes / interrupt / skill_injection / evidence。创建期 preflight 用它
与 Target adapter 的**实际声明能力**比对；不满足即拒绝，不静默改选其他
Target。没有多轮 send 能力的外部 CLI 不能伪装成可执行本 Workflow。

M4 的 interactive 命令通道不等于 TargetSession 多轮 send；只有真正实现了
send 适配器的 runtime 才可声明 multi_turn。

## 6. 失败政策与恢复

failure_policy 有两类：stop_case 与 continue_for_evidence。后者只能继续
只读检查与 cleanup，不能在安全违规后继续执行危险业务动作。

fixture/checkpoint 是审计点，不是自动续跑授权。外部动作可能已执行而确认
未落库时沿 CaseAttempt 转 needs_review；只有步骤显式声明无外部副作用且
输入/状态快照固定，才允许显式安全重放。

## 7. 旧 DSL 转换

见 docs/migration/workflow-parity.md。转换是只读的：不导入、不执行旧代码，
旧表达式只经 ast 白名单解析，setup/cleanup shell 与模型用户模拟器明确拒绝。
诊断里出现任何 error 级条目时，转换结果 publishable=False，
to_publishable_workflow() 具名拒绝，不产出“近似可运行”的已发布版本。

## 8. 验证入口

```bash
uv run pytest -q tests/contract/test_workflow_spec.py
uv run pytest -q tests/scenario/test_workflow_conversion.py
uv run pytest -q tests/storage/test_workflow_resources.py
```
