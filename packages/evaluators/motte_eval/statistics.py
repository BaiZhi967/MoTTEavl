"""统计政策 ``statistical_policy@1`` 的实现（M6 协议 §10，T06）。

统计单位、权重、区间方法、seed、分位数插值全部版本化：这些参数的任何改动
都是新政策版本（``statistical_policy@2``），不改写历史结论（协议 §9/§11）。

冻结要点（协议 §10）：

- 统计单位是 Task：比较/配对按 Task 对齐；Trial 是 Task 内样本；bootstrap
  以 Task 为重采样单元，同一 Task 的 Trial 组保持完整。
- 配对 bootstrap：默认 seed ``20260921``、2000 次重采样、95% 百分位区间；
  参数进入政策，可覆盖但覆盖值必须随结果记录。小样本（<2 Task）或无配对
  资格时只给原始差值，区间标 not applicable。
- 分位数：线性插值（numpy ``linear`` 语义，纯标准库实现）。
- 二元区间：正态近似 Wald 区间仅用于独立二元样本；关联 Task 不沿用同一假设。
- pass@k = ``1 - C(n-c, k) / C(n, k)``；仅当 n 个事前计划、独立、有效完整的
  Trial（transport / operator retry 不计）且 1<=k<=n。
- 缺失政策 keep_visible：None/NaN 不计入统计但计入 missing，缺失可见、
  永不从分母静默删除。
- 不提供未事前定义的加权总排名；综合分必须有独立版本化政策。

全部纯函数、确定性、零第三方依赖（标准库 only），可独立测试。
"""
from __future__ import annotations

import math
import random

from motte_contracts.hashing import canonical_hash

STATISTICAL_POLICY_V1: dict = {
    "policy_id": "statistical_policy@1",
    "unit": "task",
    "confidence": 0.95,
    "interval_method": "percentile_bootstrap",
    "bootstrap_iterations": 2000,
    "bootstrap_seed": 20260921,
    "quantile_interpolation": "linear",
    "binary_interval": "normal_approximation",
    "implementation_version": "motte_eval.statistics@1",
    "missing_policy": "keep_visible",
}

# 95% 正态近似（Wald）的 z 值。
_Z_95 = 1.959963984540054


def statistical_policy_hash() -> str:
    """统计政策内容的 canonical hash（协议 §8 规则，前缀 ``sha256:``）。

    政策发布后不可变；同 id 异内容是错误。结果引用方（GateResult /
    ReportSnapshot）用该 hash 固定所依据的统计语义。
    """
    return canonical_hash(STATISTICAL_POLICY_V1)


def quantile(values: list[float], q: float) -> float | None:
    """线性插值分位数（numpy ``linear`` 语义，纯 Python 实现）。

    sorted 后取位置 ``(n-1)*q``，落在两个次序统计量之间时线性内插：
    ``v[lo] + (v[hi] - v[lo]) * frac``。输入不需要已排序；空列表返回
    None；q 超出 [0, 1] 抛 ValueError。
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be within [0, 1], got {q!r}")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _visible_values(values: list[float | None]) -> tuple[list[float], int]:
    """keep_visible：None/NaN 不进统计，但保留在 missing 计数里。"""
    kept = [v for v in values if v is not None and not math.isnan(v)]
    return kept, len(values) - len(kept)


def summary_statistics(values: list[float | None]) -> dict:
    """均值/中位数/p10/p90/min/max 摘要（单位与区间语义见政策 v1）。

    None/NaN 值不计入统计但计入 ``missing``（keep_visible：缺失可见，
    不移除事实，也不折算成 0）。空列表（或全部缺失）时统计值全为 None。
    """
    kept, missing = _visible_values(values)
    return {
        "count": len(kept),
        "mean": (sum(kept) / len(kept)) if kept else None,
        "median": quantile(kept, 0.5),
        "p10": quantile(kept, 0.1),
        "p90": quantile(kept, 0.9),
        "min": min(kept) if kept else None,
        "max": max(kept) if kept else None,
        "missing": missing,
        "policy_ref": STATISTICAL_POLICY_V1["policy_id"],
    }


def _effective_bootstrap_params(
    seed: int | None, iterations: int | None
) -> tuple[int, int]:
    """解析 bootstrap 覆盖参数：默认取政策 v1，覆盖值必须为合法正数。

    覆盖（seed / iterations）允许，但结果必须记录实际使用的值（协议 §10）。
    """
    eff_seed = STATISTICAL_POLICY_V1["bootstrap_seed"] if seed is None else seed
    eff_iterations = (
        STATISTICAL_POLICY_V1["bootstrap_iterations"] if iterations is None else iterations
    )
    if eff_iterations < 1:
        raise ValueError(f"iterations must be a positive integer, got {eff_iterations!r}")
    return eff_seed, eff_iterations


def task_cluster_bootstrap(
    task_values: list[tuple[float, float]],
    *,
    seed: int | None = None,
    iterations: int | None = None,
) -> dict:
    """以 Task 为重采样单元的配对 bootstrap（协议 §10 / A13 核心）。

    输入是每个 Task 的一对 ``(baseline_value, candidate_value)``；Task 内
    多个 Trial 由调用方先聚合为一对值（或传入均值）。**调用方契约：同一
    Task 的多个 Trial 绝不能拆成多个独立样本**——重采样单元是 Task，拆开
    会虚增样本量并破坏聚类结构。

    对每个 Task i 计算差 ``d_i = candidate - baseline``；用
    ``random.Random(seed)`` 有放回重采样 n 个 Task 索引，取重采样差的均值；
    重复 iterations 次；区间取 2.5 与 97.5 百分位。固定 seed 两次调用结果
    完全相同；不同 seed 结果（几乎必然）不同。

    n_tasks < 2 时区间 not applicable（low/high 为 None），只给原始差值
    统计。seed / iterations 覆盖值原样记录在返回结果中。
    """
    eff_seed, eff_iterations = _effective_bootstrap_params(seed, iterations)
    diffs = [candidate - baseline for baseline, candidate in task_values]
    n_tasks = len(diffs)
    result: dict = {
        "mean_diff": (sum(diffs) / n_tasks) if diffs else None,
        "interval_low": None,
        "interval_high": None,
        "iterations": eff_iterations,
        "seed": eff_seed,
        "n_tasks": n_tasks,
        "applicable": False,
    }
    if n_tasks < 2:
        return result
    rng = random.Random(eff_seed)
    resampled_means: list[float] = []
    for _ in range(eff_iterations):
        sample = rng.choices(diffs, k=n_tasks)
        resampled_means.append(sum(sample) / n_tasks)
    result["interval_low"] = quantile(resampled_means, 0.025)
    result["interval_high"] = quantile(resampled_means, 0.975)
    result["applicable"] = True
    return result


def paired_difference(
    pairs: list[tuple[float, float]],
    *,
    seed: int | None = None,
    iterations: int | None = None,
) -> dict:
    """配对差统计（candidate - baseline），区间委托 Task 聚类 bootstrap。

    ``pairs`` 的每个元素是一个 Task 的 ``(baseline_value, candidate_value)``
    （Task 内多 Trial 由调用方先聚合）。n < 2 时只给原始差值统计，区间
    ``applicable=False`` 且 low/high 为 None。seed / iterations 覆盖值
    原样记录在返回的 interval 里。
    """
    eff_seed, eff_iterations = _effective_bootstrap_params(seed, iterations)
    diffs = [candidate - baseline for baseline, candidate in pairs]
    n = len(diffs)
    interval: dict = {
        "low": None,
        "high": None,
        "method": "task_cluster_bootstrap",
        "iterations": eff_iterations,
        "seed": eff_seed,
        "applicable": False,
    }
    result: dict = {
        "n": n,
        "mean_diff": (sum(diffs) / n) if diffs else None,
        "median_diff": quantile(diffs, 0.5) if diffs else None,
        "min_diff": min(diffs) if diffs else None,
        "max_diff": max(diffs) if diffs else None,
        "interval": interval,
    }
    if n >= 2:
        boot = task_cluster_bootstrap(pairs, seed=seed, iterations=iterations)
        interval["low"] = boot["interval_low"]
        interval["high"] = boot["interval_high"]
        interval["applicable"] = True
    return result


def binary_interval(successes: int, n: int) -> dict:
    """独立二元样本比例的 95% 正态近似（Wald）区间。

    ``p ± z * sqrt(p*(1-p)/n)``，z = 1.959963984540054，clip 到 [0, 1]。

    **只适用于独立二元样本**：样本间必须独立（如互不关联的单次判定）。
    关联 Task（同一 Task 的多个 Trial、同 Run 的重复观测）不满足独立性
    假设，不沿用同一方法——应改用 :func:`task_cluster_bootstrap`（协议
    §10"关联 Task 不沿用同一假设"）。n <= 0 或 successes 不在 [0, n] 时
    not applicable。
    """
    not_applicable = {
        "proportion": None,
        "low": None,
        "high": None,
        "method": "normal_approximation",
        "n": n,
        "applicable": False,
    }
    if n <= 0 or not 0 <= successes <= n:
        return not_applicable
    proportion = successes / n
    half_width = _Z_95 * math.sqrt(proportion * (1.0 - proportion) / n)
    return {
        "proportion": proportion,
        "low": max(0.0, proportion - half_width),
        "high": min(1.0, proportion + half_width),
        "method": "normal_approximation",
        "n": n,
        "applicable": True,
    }


def pass_at_k(n: int, c: int, k: int) -> dict:
    """无偏 pass@k：``1 - C(n-c, k) / C(n, k)``（用 :func:`math.comb`）。

    资格规则（A11/A12）：``1 <= k <= n`` 且 ``0 <= c <= n`` 且 n >= 1，
    否则 not applicable（``k_out_of_range`` / ``invalid_counts``），不是 0。
    n - c < k 时失败组合 C(n-c, k) = 0，值为精确 1.0（任何 k 抽取必含
    成功）。value round 到 10 位，避免浮点噪声。

    **计划不足 / 含未知 Trial 的资格判定不在本函数**——由调用方负责：
    只有事前计划、独立、有效完整的 Trial 才能作为 n 传入；transport
    retry / operator retry / 恢复 retry 不计入 n；违反独立条件或计划不足
    时调用方应判 not_applicable（协议 §10）。
    """
    if not 1 <= k <= n:
        return {"applicable": False, "value": None, "reason": "k_out_of_range"}
    if not 0 <= c <= n:
        return {"applicable": False, "value": None, "reason": "invalid_counts"}
    total_combinations = math.comb(n, k)
    fail_combinations = math.comb(n - c, k) if n - c >= k else 0
    value = round(1.0 - (fail_combinations / total_combinations), 10)
    return {"applicable": True, "value": value, "n": n, "c": c, "k": k, "reason": None}


def latency_summary(case_latencies_ms: list[float | None]) -> dict:
    """Case 级延迟摘要：p50/p90/mean（毫秒）与 missing 计数。

    None（以及 NaN，与 keep_visible 政策一致）计入 ``missing``，不进入
    分位数与均值；复用 :func:`quantile` 的线性插值语义。
    """
    kept, missing = _visible_values(case_latencies_ms)
    return {
        "p50_ms": quantile(kept, 0.5),
        "p90_ms": quantile(kept, 0.9),
        "mean_ms": (sum(kept) / len(kept)) if kept else None,
        "count": len(kept),
        "missing": missing,
    }
