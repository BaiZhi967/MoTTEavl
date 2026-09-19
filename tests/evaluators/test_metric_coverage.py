"""M6-T02 Lite：指标覆盖与合法分母。

反例与期望：
- 分母保持套件口径（GSM8K=selected、Direct=judged），不统一强改。
- 10 选中 8 已判全对 → coverage=0.8。
- 空分母/NaN 不能通过；费用 known/null 分开，零成功成本不适用。
"""
import pytest

from motte_eval.coverage import coverage_summary


def test_coverage_preserves_suite_denominators():
    gsm = coverage_summary(denominator="selected_cases", selected=10, judged=8, scored=8)
    assert gsm["denominator"] == "selected_cases"
    assert gsm["coverage"] == pytest.approx(0.8)

    direct = coverage_summary(denominator="judged_cases", selected=12, judged=9, scored=9)
    assert direct["denominator"] == "judged_cases"
    assert direct["coverage"] == pytest.approx(1.0)


def test_dispositions_are_complete_and_mutually_exclusive():
    summary = coverage_summary(
        denominator="selected_cases", selected=10, attempted=9, judged=7,
        scored=7, unknown=1, not_attempted=1, failed_after_attempt=1,
    )
    assert summary["selected"] == 10
    assert summary["attempted"] == 9
    assert summary["judged"] == 7
    assert summary["not_attempted"] == 1
    assert summary["disposition_total"] == 10  # 不缺样本
    with pytest.raises(ValueError, match="disposition"):
        coverage_summary(
            denominator="selected_cases", selected=5, attempted=5, judged=5,
            scored=5, unknown=1, not_attempted=1,
        )


def test_empty_or_nan_denominators_do_not_pass():
    empty = coverage_summary(denominator="selected_cases", selected=0, judged=0, scored=0)
    assert empty["coverage"] is None
    assert empty["passable"] is False
    nan = coverage_summary(
        denominator="selected_cases", selected=10, judged=10, scored=10,
        metric_value=float("nan"),
    )
    assert nan["metric_passable"] is False


def test_cost_known_split_and_zero_success_not_applicable():
    unknown_cost = coverage_summary(
        denominator="selected_cases", selected=4, judged=3, scored=3,
        cost={"known": False},
    )
    assert unknown_cost["cost"]["known"] is False
    assert unknown_cost["cost"]["total_usd"] is None
    assert unknown_cost["cost_passable"] is False

    known = coverage_summary(
        denominator="selected_cases", selected=4, judged=4, scored=4,
        cost={"known": True, "total_usd": 1.5, "currency": "USD"},
    )
    assert known["cost_passable"] is True

    zero_success = coverage_summary(
        denominator="selected_cases", selected=4, judged=0, scored=0,
        cost={"known": True, "total_usd": 0.0},
    )
    assert zero_success["cost"]["per_success_usd"] is None  # 不适用，不当 0
