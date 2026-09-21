# 统计政策（statistical_policy@1）

实现位于 `packages/evaluators/motte_eval/statistics.py`；权威定义见
`docs/protocols/experiments-and-comparison.md` §10（冻结）。本页是运维速查。

## v1 参数表

| 参数 | 值 | 说明 |
|---|---|---|
| `policy_id` | `statistical_policy@1` | 政策身份（版本化） |
| `unit` | `task` | 统计单位：比较/配对按 Task 对齐，Trial 是 Task 内样本 |
| `confidence` | `0.95` | 区间置信水平 |
| `interval_method` | `percentile_bootstrap` | 配对差区间方法（百分位 bootstrap） |
| `bootstrap_iterations` | `2000` | 重采样次数；可覆盖，覆盖值必须随结果记录 |
| `bootstrap_seed` | `20260921` | 默认 seed；可覆盖，覆盖值必须随结果记录 |
| `quantile_interpolation` | `linear` | numpy `linear` 语义，纯标准库实现 |
| `binary_interval` | `normal_approximation` | 独立二元样本的 Wald 正态近似 |
| `implementation_version` | `motte_eval.statistics@1` | 实现版本 |
| `missing_policy` | `keep_visible` | 缺失可见：不计入统计但计入 missing，不静默删除 |

政策内容 hash 用 `statistical_policy_hash()`（canonical JSON sha256，协议
§8 规则）计算，供 GateResult / ReportSnapshot 引用。

## 适用条件

- **Task 聚类 bootstrap**（`task_cluster_bootstrap` / `paired_difference`）：
  配对比较的标准区间方法。重采样单元是 Task——同一 Task 的多个 Trial 必须
  先聚合为一对值（或均值），绝不能拆成独立样本（虚增样本量、破坏聚类
  结构）。Task 数 < 2 时只给原始差值，区间 `not_applicable`。
- **Wald 二元区间**（`binary_interval`）：仅适用于**独立**二元样本；
  Task 间关联（同一 Task 多 Trial）不满足独立性假设，应改用 Task 聚类
  bootstrap。n ≤ 0 或 successes 越界 → `not_applicable`。
- **pass@k**（`pass_at_k`）：`1 - C(n-c, k) / C(n, k)`。只有事前计划、
  独立、有效完整的 Trial 计入 n；transport retry / operator retry /
  恢复 retry 不计。验收样例：n=5, c=2, k=2 → `1 - C(3,2)/C(5,2) =
  1 - 3/10 = 0.7`。k>n / 计划不足 / 含未知 Trial / 违反独立条件 →
  `not_applicable`（不是 0）。

## 缺失政策（keep_visible）

None / NaN 不进入均值、分位数等统计，但计入 `missing` 计数；缺失永不
从分母静默删除，空输入不折算成 0，也不产生 pass。

## 确定性

bootstrap 用 `random.Random(seed)`：固定 seed 重复调用结果逐位相同；
不同 seed 结果（几乎必然）不同。任何覆盖（seed / iterations）都写进
返回结果，保证可复核、可复现。

## 版本化规则

政策发布后不可变（协议 §9）。修改任何参数（seed、迭代数、区间方法、
分位数插值、缺失政策、适用条件等）= 新版本 `statistical_policy@2`，
同步更新 `implementation_version` 与协议文档；已持久化的历史结论、
GateResult、ReportSnapshot 引用旧 hash 不受影响，不改写历史。
