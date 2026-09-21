# Gate 与 CI 运维说明（M6）

状态：T03/T08/T10 已实现并有行为测试；正式（formal）门禁对实验性证据源保持
fail-closed。本页描述规则语义、退出码、导出格式与 CI 接入方式。

## 1. 对象与生命周期

| 对象 | 身份 | 生命周期 |
|---|---|---|
| GatePolicyVersion | `policy_id@version` | draft → published（不可变）→ deprecated（历史可读） |
| GateResult | `gate_result_id = sha256(input_hash + semantics_hash)` | 追加只读；重复求值同 id 同内容幂等 |

发布：`POST /api/v1/gate-policies`（或 `motte gate policy-publish`）。同
`(policy_id, version)` 同内容幂等，异内容 409。规则 kind、severity、
missing_policy 取值域在契约层 fail-closed（未知值 422）。

## 2. 决策与退出码（协议 §7 冻结）

| decision | 退出码 | 含义 |
|---|---|---|
| `pass` | 0 | 全部 block 规则通过 |
| `quality_fail` | 1 | 质量阈值/退化/关键样本失败 |
| （配置/请求不合法） | 2 | 求值前拒绝（未知规则/指标/方向矛盾） |
| `execution_error` | 3 | Run failed/needs_review 且规则要求执行成功 |
| （用户取消） | 4 | CLI 显式取消 |
| `insufficient_evidence` / `not_comparable` | 5 | 覆盖/样本不足、指标缺失、成本未知、不可比 |
| `safety_block` | 6 | 禁止副作用/安全标记 |

多失败并存时 JSON **永远保留全部 rule results**；进程退出码按优先级
safety_block > execution_error > insufficient/not_comparable > quality_fail > pass。
`warn` 规则失败不改决策，只记录。

## 3. fail-closed 语义（重点）

- 缺失指标值（含空分母、未评分、NaN/Infinity）→ `insufficient`，不折算 0。
- 成本未知（无价格表 / 多币种并存 / unknown usage）→ cost 规则 `insufficient`，
  严格成本门禁不能通过。
- 实际模型身份未回报 → `insufficient`（请求模型不替代回报值）。
- `side_effect_violations` / `safety_markers` 不可观测 → `insufficient`
  （"没看到违规"不等于"无违规"）。
- 未校准 Judge（<30 人工校准）或 runtime identity unknown 的规则标
  `experimental_evidence=True`：正式门禁一律 `insufficient`；只有显式
  `diagnostic=True` 的政策允许继续求值。
- `diagnostic_skip` 只允许出现在诊断政策；参与过求值时整体决策不得为
  `pass`（降为 `insufficient_evidence`）。

## 4. 求值输入与只读保证

求值固定 `RunReportRef`（run + scoring_pass + evidence hash）与
`ReportSnapshot`（dispositions / metric values / coverage / cost / evidence
pins）。**compare / gate / history / export 全程零 Provider / Judge / Runner /
业务工具调用**；Gate 不自动触发补跑或重评分——不足证据时返回
`suggested_actions`，所需动作（如共同重评分）必须显式授权执行。

Run `completed` 只代表执行终态，不代表质量通过（A17）。`needs_review` /
`failed` 的 Run 对 `requires_successful_run` 规则给 `execution_error`。

## 5. 导出（exporter v1）

- JSON：`motte gate export --format json` / `GET /api/v1/gates/{id}/export?format=json`
  — 全部规则、hash、退出码摘要（`exporter_version: gate-exporter@1`）。
- JUnit：`--format junit` — 每条规则一个 testcase；fail→failure、
  insufficient/not_applicable→error、skipped_diagnostic→skipped；顶层
  testsuite properties 带 decision/exit_code/gate_result_id/conclusion_hash。
- M7 只能在 exporter v1 上扩展，不重写主权。

## 6. CI 接入

```bash
# 固定证据求值（不启动任何 Run；M7 才有 run-and-gate）
motte gate evaluate --policy my-policy@1 --run run-xxxx [--pass pass-yyyy] \
  [--baseline blt-zzz] --json out.json        # 退出码 0-6
motte gate export --result out.json --format junit > junit.xml
```

CI 用退出码阻断（`set -e` 或显式 case 0），JUnit 供平台展示。示例 workflow
只使用合成固定证据；workflow 成功或 Run completed 不转成 Gate pass。

## 7. 回退

停新 GatePolicy 发布（deprecate 现行版本），保留全部历史 GateResult /
BaselineSnapshot / ReportSnapshot / Artifact pin。新阈值/规则/统计实现 =
新 `policy_id@version`，不重写旧政策或历史结论。
