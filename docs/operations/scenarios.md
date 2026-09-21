# 场景（Scenario）运维说明（M5）

状态：T01/T03b/T05/T07/T08 已实现并有测试；公开执行链路（R6）已闭环并有行为
测试：公共 API 创建 → 持久 queued Run → 普通 WorkerLoop 领取 → 冻结证据 → 既有
评分入口 → 报告 → 离线 rescore（tests/integration/test_scenario_run_backend.py、
tests/integration/test_business_regression_slice.py）。SCENARIO_BACKEND_AVAILABLE=True；
创建期仍然具名拒绝不满足能力的配置，显式 install_scenario_backend(available=False)
可以关闭新执行，不静默改选其他 backend。

## 1. 对象与身份

- ScenarioVersion：dataset / model / runtime / skill / evaluator / 预算引用。
- WorkflowVersion：如何逐步驱动目标，存储键 (workflow_id, version)，发布即不可变。
- FixtureVersion：初始状态与允许工具，存储键 (fixture_id, version)。
- 一个 Run 的 execution backend 是 scenario@1；内层 target（例如 builtin-agent@1
  或 pi-agent@1）是另一个身份，单独冻结在 manifest.agent / manifest.runtime。
- 评分器身份也冻结：Scenario 资源里的 `evaluator` 声明在创建期写入
  `manifest.resource_snapshots.workflow_evaluator`（带 config_sha256），并投影成
  `manifest.evaluation`（scorer_id/scorer_version）。客户端提交 `resource_snapshots`
  或 `evaluation` 一律 SNAPSHOT_RESERVED 拒绝。

## 2. 发布

```bash
uv run pytest -q tests/contract/test_workflow_spec.py tests/scenario
```

发布通过 ResourceStore.publish_workflow / publish_fixture（逐版本不可变，同内容
幂等、异内容冲突、不可删除）。内容 hash 自洽是入库条件，草稿不能发布。

## 3. 旧 DSL 迁移

只读转换，输出诊断与映射表；任何 error 级诊断都会让 publishable=False 并具名
拒绝。旧表达式只经 ast 白名单解析，绝不 eval；setup/cleanup shell 与模型用户
模拟器明确拒绝。详见 docs/migration/workflow-parity.md。

## 4. 执行与证据

一个 CaseAttempt 装配一个 FixtureInstance + 一个 TargetSession + 一个引擎实例：

- 事件带 run/case/attempt/step 身份，进入既有 RunService 事件通道；
- checkpoint 以 Artifact（含内容 hash、owner、step）冻结；
- 目标只拿到受控工具桥，工具结果只带最小业务可见字段；隐藏字段（gold /
  checker 真值 / 隐藏断言）在**任意嵌套层级**都被剔除，投影后还有隐藏键就
  具名拒绝（SCENARIO_HIDDEN_VALUE_LEAK），不会静默放行；
- 超时后必须确认受控进程停止，否则保留资源并转 needs_review。

### 4.1 期限

步骤期限与全局 wall_time 都是**单调时钟上的绝对期限**，并且在动作边界上被
消费，而不是"返回后再看耗时"：

- 模型轮次：`TargetSession.send(message, deadline=...)` 把剩余期限交给运行时；
  期限内模型调用用线程期限强制，期限已过不再发请求，模型响应在期限之后到达
  就不再执行任何工具（含写工具），停止原因是 `per_call_timeout`；
- fixture 工具与事件：分派前核验期限（已过期不执行、按超时处理），返回后复核
  期限；越过期限的那一步记为 failed，Workflow 不可能 completed（副作用本身
  仍如实写入工具结果，不伪报成功）。

### 4.2 工具模式（real / mock / replay / deny）

四种模式各自消费**自己的**来源，未配置支持即具名拒绝，绝不回退到真实 handler：

| mode | 来源 | 未配置时 |
|---|---|---|
| real | `tool_handlers` | `SCENARIO_TOOL_NOT_IMPLEMENTED` |
| mock | `mock_handlers`（显式模拟实现，可改自己的合成状态） | `SCENARIO_TOOL_MOCK_MISSING` |
| replay | `replay_records`（冻结记录 `{"result": ..., "state": ...}`，不重放业务动作） | `SCENARIO_TOOL_REPLAY_MISSING` |
| deny | 无（直接拒绝） | `SCENARIO_TOOL_DENIED` |

公共入口用 `motte_sdk.scenario_backend.register_scenario_tools(fixture_id, handlers=...,
mock_handlers=..., replay_records=..., event_handlers=...)` 登记实现（与 target adapter
同一层：策略由冻结资源决定，实现由部署登记）。没有登记的 fixture 工具在公共路径上
具名拒绝；两个 fixture 声明同名工具且实现不同是 `SCENARIO_TOOL_HANDLER_CONFLICT`。

目标自身的工具调用走同一个受控桥，mode 取自冻结的
`manifest.target_snapshot.tool_modes`：声明里包含 real 才允许 real；只声明一个
其他模式时整场用该模式；既没有 real 又不止一个模式时有效 mode 无法证明，
具名拒绝（`SCENARIO_TOOL_MODE_AMBIGUOUS`），不猜。

### 4.3 失败政策与清理

- 全局 `WorkflowVersion.failure_policy=continue_for_evidence` 被引擎消费：
  失败后只读步骤（assert / checkpoint）继续执行，写工具、事件与 send_message
  记为 skipped，终局仍是失败。步骤级 `continue_for_evidence` 仍然生效；步骤级
  `stop_case` 不能取消全局的继续取证（冻结快照里契约默认值与显式声明不可
  区分，取更严格的一方）。
- 清理只在**确认停止**后进行：`needs_review` 或中断确认
  `confirmed=false`（含 close 失败）时保留现场——实例根、state 与
  `_instance.json` 所有权标记都在，清理记录与
  `<anchor>/_cleanup/<instance_id>.json` 审计证据同时落盘。
- 逐个 fixture 的清理所有权在 prepare 成功时立刻移交：第二个 fixture 失败时
  第一个已成功实例也会被清理并留证；装配/执行抛错时原始异常带上
  `cleanups`（与 `__notes__`），原错误与清理结论都不丢。

### 4.4 冻结 Observation 与评分装配（R6）

执行结束前，执行器把证据物化成**既有契约** `FrozenObservation`，放在 Case 结果的
`frozen_observation` 字段（人读的 `observation`（workflow-observation@1）不变）：

- 每个 checkpoint 一个 `state` 证据：Artifact 字节 = 该 checkpoint 的可见业务状态，
  带 owner(run/case/attempt/fixture)、step、artifact_sha256 与 payload_schema 摘要；
- 一份有序业务动作日志 `action-log`（`action_log` 证据）：每次受控工具调用的
  action/target/status/call_id/step，**包含被权限拒绝的尝试**（status=denied）；
- `complete=false` 表示证据不完整（停止未确认 / needs_review），此时不允许任何
  否定结论；
- `coverage` / `tool_calls` / `event_refs` 与 `evidence_hash` 绑定同一份评分视图。

评分走既有入口 `RunService._score_results`：scenario Run 由
`motte_sdk.scenario_backend.scenario_scores` 组装指标并调用
`motte_eval.observation.evaluate_observation`（`motte_eval.workflow` 的
state-equals / state-delta / no-side-effect / goal-achieved / response-policy 在此注册）。
评分前校验 Observation 的 evidence_hash 重算值、run/case 归属与每条 workflow_evidence
的 owner；任何不符只产生 `insufficient_evidence` 行，零调用、零 pass。
离线 rescore 读取同一份已持久化证据，不重开 Target、不调模型、不碰业务工具。

指标配置来自 Scenario 资源的 `evaluator`：

```json
{
  "name": "order-cancel", "version": "1",
  "evaluator": {
    "evaluator_id": "workflow-assertions", "version": "1",
    "config": {"metrics": [
      {"metric_id": "confirm-before-cancel", "kind": "response-policy",
       "log": "action-log", "ordered": true,
       "require": [{"action": "orders.get", "target": "order-1", "min_count": 1},
                    {"action": "orders.cancel", "target": "order-1", "max_count": 1}]},
      {"metric_id": "order-cancelled", "kind": "state-equals",
       "checkpoint": "final-check", "path": "order.status", "expected": "cancelled"},
      {"metric_id": "no-refund", "kind": "no-side-effect", "scope": ["actions"],
       "log": "action-log", "forbid": [{"action": "orders.refund", "status": "succeeded"}]},
      {"metric_id": "goal", "kind": "goal-achieved", "components": [
        {"role": "final_state", "metric": {"metric_id": "g1", "kind": "state-equals",
         "checkpoint": "final-check", "path": "order.status", "expected": "cancelled"}},
        {"role": "process", "metric": {"metric_id": "g2", "kind": "response-policy",
         "log": "action-log", "require": [{"action": "orders.cancel", "min_count": 1}]}}]}
    ]}
  }
}
```

约定：checkpoint 证据 id 就是步骤 `step_id`；业务动作日志的 id 固定为 `action-log`，
`monitored_scope=["business_actions"]`。配置在创建期用既有 `normalize_evaluator_config`
校验（非法即 `WORKFLOW_EVALUATOR_INVALID`），冻结后评分不再解析资源；没有声明
`evaluator` 的 Scenario 不产生任何评分行（不发明 pass）。

### 4.5 无 fixture 的纯消息流程

Workflow 没有 `fixture_refs`、也没有 `invoke_fixture_tool` / `trigger_fixture_event`
步骤时不需要固定 Fixture：执行器使用空状态端口（目标拿不到任何业务工具），
checkpoint 仍可取证（空状态）。创建期与执行期共用同一个判断
（`motte_scenario.executor.workflow_requires_fixture`），因此 Run 不会被"先接受、
执行时才说缺 primary binding"。

## 5. 已知缺口

1. 公开执行只覆盖公共路径已交付的部分：needs_review 的 Case 会让 Run 以
   completed + insufficient 评分收尾（现场保留、不自动重放），Run 级
   needs_review 转换留给后续收口。
2. 生产环境仍需按 fixture 登记真实业务工具实现；未登记的工具/事件具名拒绝，
   不伪造成功。
3. Skill 注入与三臂对照（T07/T08）不在本包范围内。
