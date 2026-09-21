# 场景（Scenario）运维说明（M5）

状态：T01/T03b/T05/T07/T08 已实现并有测试；公开执行在纵向评分链路接通前
保持关闭（SCENARIO_BACKEND_AVAILABLE=False），创建期返回
EXECUTION_BACKEND_UNAVAILABLE，不静默改选其他 backend。

## 1. 对象与身份

- ScenarioVersion：dataset / model / runtime / skill / evaluator / 预算引用。
- WorkflowVersion：如何逐步驱动目标，存储键 (workflow_id, version)，发布即不可变。
- FixtureVersion：初始状态与允许工具，存储键 (fixture_id, version)。
- 一个 Run 的 execution backend 是 scenario@1；内层 target（例如 builtin-agent@1
  或 pi-agent@1）是另一个身份，单独冻结在 manifest.agent / manifest.runtime。

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

目标自身的工具调用走同一个受控桥，mode 取自冻结的
`manifest.target_snapshot.tool_modes`：声明里包含 real 才允许 real；只声明一个
其他模式时整场用该模式；既没有 real 又不止一个模式时有效 mode 无法证明，
具名拒绝（`SCENARIO_TOOL_MODE_AMBIGUOUS`），不猜。

### 4.3 失败政策与清理

- 全局 `WorkflowVersion.failure_policy=continue_for_evidence` 现在被引擎消费：
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

## 5. 已知缺口

1. scoring 投影（Observation → ScoreSet/ScoringPass）尚未接通，因此公开执行保持关闭。
2. 生产环境尚未注册 fixture 工具实现；未实现的工具/事件具名拒绝，不伪造成功。
3. 平台侧 fixture 工具 step 与目标桥已按 real/mock/replay/deny 分派；mock 实现与
   replay 记录由装配方显式提供，公开入口尚未接线（R6/R7）。