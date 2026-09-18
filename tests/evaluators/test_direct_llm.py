"""Direct LLM 通用评分器与聚合口径（纯函数，零调用）。"""
import pytest

from motte_contracts.direct_llm import SCORER_VERSION
from motte_eval.direct_llm import aggregate_answers, matches, score_answer_case
from motte_eval.execution import CONTINUE_ERROR_CLASSES


@pytest.mark.parametrize("content,expected,scorer,passed", [
    ("北京", "北京", "exact", True),
    ("  北京\n", "北京", "exact", True),
    ("北京市", "北京", "exact", False),
    ("Beijing", "北京", "exact", False),
    ("标签是 billing。", "billing", "contains", True),
    ("BILLING", "billing", "contains", False),
    ("", "billing", "contains", False),
    ('{"amount": 1280}', r'"amount"\s*:\s*1280', "regex", True),
    ("1280 元", r'"amount"\s*:\s*\d+', "regex", False),
    ("公告已发。 [SUMMARY]", r"\[SUMMARY\]", "regex", True),
])
def test_scorer_semantics(content, expected, scorer, passed):
    assert matches(content, expected, scorer) is passed


def test_matches_rejects_unknown_scorer():
    with pytest.raises(ValueError, match="unsupported scorer"):
        matches("a", "a", "fuzzy")


def test_score_answer_case_outcomes():
    correct = score_answer_case({"content": "北京"}, "北京", "exact")
    assert correct == {"passed": True, "attempted": True, "responded": True, "judged": True,
                       "scorer": "exact", "scorer_version": SCORER_VERSION, "outcome": "correct"}
    wrong = score_answer_case({"content": "上海"}, "北京")
    assert wrong["outcome"] == "wrong_answer" and wrong["passed"] is False
    assert wrong["scorer"] == "exact"  # 省略评分器时用契约默认值
    unjudged = score_answer_case({"content": "随便"}, None)
    assert unjudged["outcome"] == "no_expectation" and unjudged["judged"] is False
    assert unjudged["attempted"] is True and unjudged["passed"] is False
    blank = score_answer_case({"content": "x"}, "   ")
    assert blank["outcome"] == "no_expectation"
    failed = score_answer_case({"error": {"class": "rate_limit", "message": "slow"}}, "北京")
    assert failed["outcome"] == "call_failed" and failed["responded"] is False
    assert failed["judged"] is True and failed["error_class"] == "rate_limit"
    # 缺少期望的题即使调用失败也不进判定分母
    assert score_answer_case({"error": {"class": "timeout"}}, None)["judged"] is False
    # 非 envelope 的裸字符串结果按内容比较
    assert score_answer_case("北京", "北京")["outcome"] == "correct"
    assert score_answer_case(None, "北京")["outcome"] == "wrong_answer"


def test_aggregate_uses_judged_denominator():
    scores = [
        {"case_id": "a", "outcome": "correct", "attempted": True, "responded": True, "judged": True},
        {"case_id": "b", "outcome": "wrong_answer", "attempted": True, "responded": True, "judged": True},
        {"case_id": "c", "outcome": "call_failed", "attempted": True, "responded": False, "judged": True},
        {"case_id": "d", "outcome": "no_expectation", "attempted": True, "responded": True,
         "judged": False},
        {"case_id": "e", "outcome": "not_attempted", "attempted": False, "responded": False,
         "judged": False},
    ]
    summary = aggregate_answers(scores, 5)
    assert summary["selected"] == 5 and summary["judged"] == 3
    assert summary["correct"] == 1 and summary["wrong_answer"] == 1 and summary["call_failed"] == 1
    assert summary["no_expectation"] == 1 and summary["not_attempted"] == 1
    assert summary["attempted"] == 4 and summary["responded"] == 3
    assert summary["accuracy"] == pytest.approx(1 / 3)
    assert summary["completion"] == pytest.approx(1.0)
    assert summary["attempt_rate"] == pytest.approx(4 / 3)
    assert summary["scorer_version"] == SCORER_VERSION


def test_aggregate_without_any_expectation_has_no_accuracy():
    summary = aggregate_answers(
        [{"case_id": "a", "outcome": "no_expectation", "attempted": True, "responded": True,
          "judged": False}], 1)
    assert summary["judged"] == 0
    assert summary["accuracy"] is None and summary["completion"] is None
    assert summary["attempt_rate"] is None


def test_continue_error_classes_are_shared():
    assert CONTINUE_ERROR_CLASSES == frozenset({"rate_limit", "server", "timeout", "network"})
    from motte_eval.gsm8k import CONTINUE_ERROR_CLASSES as reexported

    assert reexported is CONTINUE_ERROR_CLASSES
