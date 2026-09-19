# CMMLU 外部基准操作指南（M2-T11）

> 状态：**身份/数据/验证与 C-Eval 完全隔离的 job-based 链路已交付
> （离线测试验证）；真实 Runner / 真实模型验收未执行（not_run）**。
> CMMLU 不继承 C-Eval 的任何通过状态；两套验收独立记录。

## 1. 与 C-Eval 的关系

- 共享：外部 Job 基础设施（ExternalJob 契约、进程适配器生命周期、
  持久化/幂等导入、取消/恢复语义）与两段式答案提取算法。
- 独立：benchmark id（`cmmlu@1`）、数据 revision 命名空间、Profile
  （split=test/dev、67 学科白名单与中文名、aggregation=sample-weighted-only）、
  样本清单与准备状态（Catalog 独立条目）、验证记录。
- 聚合不套用 C-Eval 四大类：未知学科不报错（`parse_opencompass_results
  (dataset="cmmlu")` 按学科宏平均 + subject 条目输出）。

## 2. 使用（与 C-Eval 同一 API 面，benchmark 维度区分）

```python
from motte_sdk.benchmark_catalog import prepare_external_dataset

prepared = prepare_external_dataset(
    benchmark_id="cmmlu",
    files={"cmmlu_test.jsonl": jsonl_bytes},
    dataset_revision="cmmlu-rev-1",   # 独立 revision，禁止复用 ceval 的
)
```

- 本地数据 → user-supplied；官方溯源需受信核验器（当前未部署 → 阻断，
  与 C-Eval 的治理互不代读）。
- Profile：`cmmlu_external_profile(subjects=..., split="test",
  few_shot_split="dev", ...)`；学科白名单 67 项（中文名迁移自旧适配器
  b661bcdf，与 opencompass 官方 cmmlu 配置对齐）。

## 3. 分层验收（独立于 C-Eval）

| 层 | 状态 | 复现 |
|---|---|---|
| 身份/治理/聚合隔离（离线） | ✅ | `uv run pytest -q -m "not live" tests/benchmarks/test_cmmlu_external_profile.py` |
| 假 Runner 全链路 | ✅（复用 ceval 集成层的通用基础设施；`tests/integration/test_ceval_external_job.py` 验证的机制同样适用于 cmmlu 适配器装配） | — |
| 真实固定 Runner + 本地确定性端点 | **not_run** | 同 `docs/operations/ceval.md` 第 4 节清单，独立记录原始 hash |
| 真实模型 smoke / full | **not_run / blocked** | 需显式授权；CMMLU 官方数据（CC-BY-NC-SA-4.0）获取与分发约束由操作员核对 |

## 4. 回退

不在 Runner 环境注册 cmmlu 适配器（或 `unregister_adapter`）即可停用；
C-Eval 与其它套件不受影响。历史 Job/工件/评分保留。
