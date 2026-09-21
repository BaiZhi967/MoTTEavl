"""M6-T06：统计政策 statistical_policy@1 的纯函数实现（协议 §10）。

反例与期望：
- pass@k 验收样例 n=5, c=2, k=2 精确 0.7；k 越界 / 计数非法 → not_applicable
  （不是 0）；n-c<k（任何 k 抽取必含成功）→ 1.0。
- bootstrap 固定 seed 两次调用逐位相同；不同 seed 区间不同。
- 单 Task 不给区间（applicable=False），只给原始差值。
- keep_visible：None/NaN 不进统计但计入 missing，输入事实不被移除。
"""
import math
import random

import pytest

from motte_contracts.hashing import canonical_hash
from motte_eval.statistics import (
    STATISTICAL_POLICY_V1,
    binary_interval,
    latency_summary,
    paired_difference,
    pass_at_k,
    quantile,
    statistical_policy_hash,
    summary_statistics,
    task_cluster_bootstrap,
)

Z_95 = 1.959963984540054

# 5 个 Task 的配对值（差值混合正负，保证不同 seed 的区间可分辨）。
PAIRS_5 = [(0.2, 0.5), (0.4, 0.3), (0.6, 0.9), (0.1, 0.7), (0.5, 0.55)]


# ---------------------------------------------------------------------------
# 政策常量与 hash
# ---------------------------------------------------------------------------

def test_statistical_policy_frozen_values():
    assert STATISTICAL_POLICY_V1 == {
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


def test_statistical_policy_hash_stable():
    first = statistical_policy_hash()
    second = statistical_policy_hash()
    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64
    # 独立用协议 §8 规则复算，钉住 hash 语义。
    assert first == canonical_hash(STATISTICAL_POLICY_V1)


# ---------------------------------------------------------------------------
# quantile：线性插值（numpy 'linear' 语义）
# ---------------------------------------------------------------------------

def test_quantile_linear_interpolation():
    values = [1.0, 2.0, 3.0, 4.0]
    assert quantile(values, 0.5) == 2.5
    assert quantile(values, 0.25) == 1.75
    assert quantile(values, 0.0) == 1.0
    assert quantile(values, 1.0) == 4.0
    assert quantile(values, 0.1) == pytest.approx(1.3)
    # 输入不需要已排序。
    assert quantile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.5
    # 单值列表任何分位都是该值。
    assert quantile([5.0], 0.0) == 5.0
    assert quantile([5.0], 1.0) == 5.0


def test_quantile_empty_and_bounds():
    assert quantile([], 0.5) is None
    with pytest.raises(ValueError):
        quantile([1.0, 2.0], 1.5)
    with pytest.raises(ValueError):
        quantile([1.0, 2.0], -0.1)


# ---------------------------------------------------------------------------
# summary_statistics：keep_visible 缺失政策
# ---------------------------------------------------------------------------

def test_summary_statistics_basic():
    result = summary_statistics([1.0, 2.0, 3.0, 4.0])
    assert result["count"] == 4
    assert result["mean"] == 2.5
    assert result["median"] == 2.5
    assert result["p10"] == pytest.approx(1.3)
    assert result["p90"] == pytest.approx(3.7)
    assert result["min"] == 1.0
    assert result["max"] == 4.0
    assert result["missing"] == 0
    assert result["policy_ref"] == "statistical_policy@1"


def test_summary_statistics_empty_list_all_none():
    result = summary_statistics([])
    assert result == {
        "count": 0,
        "mean": None,
        "median": None,
        "p10": None,
        "p90": None,
        "min": None,
        "max": None,
        "missing": 0,
        "policy_ref": "statistical_policy@1",
    }


def test_summary_statistics_none_and_nan_counted_missing_not_stats():
    values = [1.0, None, float("nan"), 3.0]
    result = summary_statistics(values)
    assert result["count"] == 2
    assert result["missing"] == 2
    assert result["mean"] == pytest.approx(2.0)
    assert result["median"] == 2.0
    assert result["p10"] == pytest.approx(1.2)
    assert result["p90"] == pytest.approx(2.8)
    assert result["min"] == 1.0
    assert result["max"] == 3.0
    # keep_visible：缺失可见，不移除输入事实（NaN 与自身不等，逐项检查）。
    assert len(values) == 4
    assert values[0] == 1.0
    assert values[1] is None
    assert math.isnan(values[2])
    assert values[3] == 3.0


# ---------------------------------------------------------------------------
# task_cluster_bootstrap：Task 为重采样单元
# ---------------------------------------------------------------------------

def test_task_cluster_bootstrap_deterministic_same_seed():
    first = task_cluster_bootstrap(PAIRS_5)
    second = task_cluster_bootstrap(PAIRS_5)
    assert first == second
    assert first["applicable"] is True
    assert first["n_tasks"] == 5
    assert first["interval_low"] <= first["interval_high"]


def test_task_cluster_bootstrap_different_seed_different_interval():
    first = task_cluster_bootstrap(PAIRS_5, seed=1)
    second = task_cluster_bootstrap(PAIRS_5, seed=2)
    assert first["mean_diff"] == second["mean_diff"]  # 原始差值统计与 seed 无关
    assert (first["interval_low"], first["interval_high"]) != (
        second["interval_low"],
        second["interval_high"],
    )


def test_task_cluster_bootstrap_single_task_not_applicable():
    result = task_cluster_bootstrap([(0.4, 0.6)])
    assert result["applicable"] is False
    assert result["interval_low"] is None
    assert result["interval_high"] is None
    assert result["n_tasks"] == 1
    assert result["mean_diff"] == pytest.approx(0.2)
    # 空输入同样只有 not_applicable 区间。
    empty = task_cluster_bootstrap([])
    assert empty["applicable"] is False
    assert empty["interval_low"] is None
    assert empty["mean_diff"] is None
    assert empty["n_tasks"] == 0


def test_task_cluster_bootstrap_defaults_and_override_recorded():
    default = task_cluster_bootstrap(PAIRS_5)
    assert default["seed"] == 20260921
    assert default["iterations"] == 2000
    override = task_cluster_bootstrap(PAIRS_5, seed=7, iterations=50)
    assert override["seed"] == 7
    assert override["iterations"] == 50
    # 覆盖参数下固定 seed 仍然逐位可复现。
    assert override == task_cluster_bootstrap(PAIRS_5, seed=7, iterations=50)


def test_task_cluster_bootstrap_resampling_semantics_pinned():
    # 用 stdlib 直接复算：有放回重采样 Task 差值取均值，区间取 2.5/97.5 百分位。
    tasks = [(0.0, 1.0), (0.0, 3.0)]  # 每个 Task 的差值 [1.0, 3.0]
    result = task_cluster_bootstrap(tasks, seed=42, iterations=3)
    rng = random.Random(42)
    diffs = [1.0, 3.0]
    expected_means = [sum(rng.choices(diffs, k=2)) / 2 for _ in range(3)]
    assert result["mean_diff"] == pytest.approx(2.0)
    assert result["interval_low"] == quantile(expected_means, 0.025)
    assert result["interval_high"] == quantile(expected_means, 0.975)


def test_task_cluster_bootstrap_rejects_non_positive_iterations():
    with pytest.raises(ValueError):
        task_cluster_bootstrap(PAIRS_5, iterations=0)


# ---------------------------------------------------------------------------
# paired_difference：配对差（candidate - baseline）
# ---------------------------------------------------------------------------

PAIRS_3 = [(1.0, 2.0), (3.0, 3.5), (2.0, 4.0)]  # 差值 [1.0, 0.5, 2.0]


def test_paired_difference_small_n_interval_not_applicable():
    single = paired_difference([(1.0, 3.0)])
    assert single["n"] == 1
    assert single["mean_diff"] == pytest.approx(2.0)
    assert single["median_diff"] == pytest.approx(2.0)
    assert single["min_diff"] == pytest.approx(2.0)
    assert single["max_diff"] == pytest.approx(2.0)
    assert single["interval"]["applicable"] is False
    assert single["interval"]["low"] is None
    assert single["interval"]["high"] is None
    assert single["interval"]["method"] == "task_cluster_bootstrap"

    empty = paired_difference([])
    assert empty["n"] == 0
    assert empty["mean_diff"] is None
    assert empty["interval"]["applicable"] is False


def test_paired_difference_interval_delegates_to_task_bootstrap():
    result = paired_difference(PAIRS_3)
    boot = task_cluster_bootstrap(PAIRS_3)
    interval = result["interval"]
    assert result["n"] == 3
    assert result["mean_diff"] == pytest.approx((1.0 + 0.5 + 2.0) / 3)
    assert result["median_diff"] == pytest.approx(1.0)
    assert result["min_diff"] == pytest.approx(0.5)
    assert result["max_diff"] == pytest.approx(2.0)
    assert interval["applicable"] is True
    assert interval["method"] == "task_cluster_bootstrap"
    assert interval["iterations"] == 2000
    assert interval["seed"] == 20260921
    assert interval["low"] == boot["interval_low"]
    assert interval["high"] == boot["interval_high"]
    assert interval["low"] <= interval["high"]


def test_paired_difference_override_recorded_in_interval():
    result = paired_difference(PAIRS_3, seed=5, iterations=10)
    assert result["interval"]["seed"] == 5
    assert result["interval"]["iterations"] == 10
    boot = task_cluster_bootstrap(PAIRS_3, seed=5, iterations=10)
    assert result["interval"]["low"] == boot["interval_low"]
    assert result["interval"]["high"] == boot["interval_high"]


# ---------------------------------------------------------------------------
# binary_interval：独立二元样本的 Wald 正态近似
# ---------------------------------------------------------------------------

def test_binary_interval_known_values():
    result = binary_interval(8, 10)
    assert result["applicable"] is True
    assert result["method"] == "normal_approximation"
    assert result["n"] == 10
    assert result["proportion"] == pytest.approx(0.8)
    half = Z_95 * math.sqrt(0.8 * 0.2 / 10)
    assert result["low"] == pytest.approx(0.8 - half)
    assert result["low"] < 0.8 < result["high"]
    # 上界超出 1 时 clip 到 [0, 1]。
    assert result["high"] == 1.0


def test_binary_interval_unclipped_two_sided():
    result = binary_interval(50, 100)
    half = Z_95 * math.sqrt(0.5 * 0.5 / 100)
    assert result["proportion"] == pytest.approx(0.5)
    assert result["low"] == pytest.approx(0.5 - half)
    assert result["high"] == pytest.approx(0.5 + half)
    assert 0.0 <= result["low"] < result["high"] <= 1.0


def test_binary_interval_not_applicable():
    empty = binary_interval(0, 0)
    assert empty["applicable"] is False
    assert empty["proportion"] is None
    assert empty["low"] is None
    assert empty["high"] is None
    assert empty["method"] == "normal_approximation"
    negative = binary_interval(1, -1)
    assert negative["applicable"] is False
    # 计数越界 fail-closed（成功数不在 [0, n]）。
    overflow = binary_interval(11, 10)
    assert overflow["applicable"] is False


def test_binary_interval_degenerate_extremes():
    full = binary_interval(10, 10)
    assert full["applicable"] is True
    assert full["proportion"] == 1.0
    assert full["low"] == 1.0
    assert full["high"] == 1.0
    none_ok = binary_interval(0, 10)
    assert none_ok["proportion"] == 0.0
    assert none_ok["low"] == 0.0
    assert none_ok["high"] == 0.0


# ---------------------------------------------------------------------------
# pass_at_k：无偏估计与资格规则（A11/A12）
# ---------------------------------------------------------------------------

def test_pass_at_k_acceptance_sample():
    result = pass_at_k(5, 2, 2)
    assert result["applicable"] is True
    assert abs(result["value"] - 0.7) < 1e-9
    # 公式钉住：1 - C(3,2)/C(5,2) = 1 - 3/10。
    assert result["value"] == round(1 - math.comb(3, 2) / math.comb(5, 2), 10)
    assert result == {
        "applicable": True, "value": 0.7, "n": 5, "c": 2, "k": 2, "reason": None,
    }


def test_pass_at_k_k_out_of_range():
    for k in (6, 0, -1):
        assert pass_at_k(5, 2, k) == {
            "applicable": False, "value": None, "reason": "k_out_of_range",
        }
    # n<1 时 1<=k<=n 无法成立，同样 k 越界。
    assert pass_at_k(0, 0, 1)["reason"] == "k_out_of_range"


def test_pass_at_k_invalid_counts():
    for c in (6, -1):
        assert pass_at_k(5, c, 2) == {
            "applicable": False, "value": None, "reason": "invalid_counts",
        }


def test_pass_at_k_all_draws_include_success():
    # n-c < k：失败组合 C(n-c, k)=0，任何 k 抽取必含成功 → 精确 1.0。
    assert pass_at_k(5, 4, 2)["value"] == 1.0
    assert pass_at_k(5, 5, 2)["value"] == 1.0
    assert pass_at_k(5, 2, 5)["value"] == 1.0


def test_pass_at_k_zero_success_and_rounding():
    # 全失败 → 0.0（公式精确值，不是缺失）。
    assert pass_at_k(5, 0, 2)["value"] == 0.0
    # 非 dyadic 结果 round 到 10 位，消除浮点噪声。
    expected = round(1 - math.comb(7, 3) / math.comb(10, 3), 10)
    result = pass_at_k(10, 3, 3)
    assert result["applicable"] is True
    assert result["value"] == expected


# ---------------------------------------------------------------------------
# latency_summary
# ---------------------------------------------------------------------------

def test_latency_summary():
    result = latency_summary([10.0, None, 30.0, 20.0])
    assert result == {
        "p50_ms": 20.0, "p90_ms": 28.0, "mean_ms": 20.0, "count": 3, "missing": 1,
    }


def test_latency_summary_all_missing_and_empty():
    all_missing = latency_summary([None, None])
    assert all_missing == {
        "p50_ms": None, "p90_ms": None, "mean_ms": None, "count": 0, "missing": 2,
    }
    empty = latency_summary([])
    assert empty == {
        "p50_ms": None, "p90_ms": None, "mean_ms": None, "count": 0, "missing": 0,
    }
