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
- 目标只拿到受控工具桥，工具结果只带最小业务可见字段；
- 超时后必须确认受控进程停止，否则保留资源并转 needs_review。

## 5. 已知缺口

1. scoring 投影（Observation → ScoreSet/ScoringPass）尚未接通，因此公开执行保持关闭。
2. 生产环境尚未注册 fixture 工具实现；未实现的工具/事件具名拒绝，不伪造成功。
3. 平台侧 fixture 工具 step 支持 real/mock/replay/deny 四种 mode；目标自身调用只走 real。
