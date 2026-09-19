"""Direct LLM 通用确定性评分：exact / contains / regex（纯函数，无调用）。

三种评分器都在「模型输出 vs 题面期望」上做确定性判定，口径写死在 ``scorer_version`` 里：

- ``exact``：两侧 strip 后完全相等（大小写敏感）。
- ``contains``：期望串是输出的子串（大小写敏感）。
- ``regex``：``re.search(期望串, 输出)``；正则在导入期就已编译校验，运行期不会因坏正则失败。

没有 ``expected`` 的题记为 ``no_expectation``：不判对错、不进 accuracy 分母。
"""
from __future__ import annotations

import re
from typing import Any

from motte_contracts.direct_llm import DEFAULT_SCORER, SCORER_VERSION, normalize_scorer


def matches(content: str, expected: str, scorer: str) -> bool:
    """单题判定；评分器取值由契约层保证已校验。"""
    if scorer == "exact":
        return content.strip() == expected.strip()
    if scorer == "contains":
        return expected in content
    if scorer == "regex":
        return re.search(expected, content) is not None
    raise ValueError(f"unsupported scorer: {scorer!r}")


def score_answer_case(result: Any, expected: Any, scorer: str | None = None) -> dict[str, Any]:
    error = result.get("error") if isinstance(result, dict) else None
    has_expected = isinstance(expected, str) and bool(expected.strip())
    score: dict[str, Any] = {"passed": False, "attempted": True, "responded": not bool(error),
                             "judged": has_expected, "scorer": normalize_scorer(scorer),
                             "scorer_version": SCORER_VERSION}
    if error:
        return {**score, "outcome": "call_failed", "error_class": error.get("class")}
    if not has_expected:
        return {**score, "outcome": "no_expectation"}
    content = result.get("content") if isinstance(result, dict) else result
    passed = matches(content if isinstance(content, str) else "", expected,
                     normalize_scorer(scorer or DEFAULT_SCORER))
    return {**score, "outcome": "correct" if passed else "wrong_answer", "passed": passed}


def aggregate_answers(scores: list[dict[str, Any]], selected_count: int) -> dict[str, Any]:
    """聚合 v1 分数，保持历史字段分母，仅修复 judged 的事实来源。

    ``judged`` 必须来自单题评分写入的标志，不能由 outcome 反推：没有期望的调用失败题
    也是 ``call_failed``，但不应进入 accuracy 分母。有期望的调用失败题仍由其
    ``judged=True`` 计入分母。v1 的 completion/attempt_rate 历史上也使用 judged 分母，
    即使可能大于 1 也不在原版本内改义；v2 才改为 selected 分母并增加 coverage。
    """
    counts = {name: sum(score["outcome"] == name for score in scores)
              for name in ("correct", "wrong_answer", "no_expectation", "call_failed",
                           "not_attempted")}
    judged_scores = [score for score in scores if score.get("judged") is True]
    judged = len(judged_scores)
    judged_correct = sum(score["outcome"] == "correct" for score in judged_scores)
    attempted = sum(bool(score.get("attempted")) for score in scores)
    responded = sum(bool(score.get("responded")) for score in scores)
    return {**counts, "selected": selected_count, "judged": judged, "attempted": attempted,
            "responded": responded,
            "completion": responded / judged if judged else None,
            "attempt_rate": attempted / judged if judged else None,
            "accuracy": judged_correct / judged if judged else None,
            "scorer_version": SCORER_VERSION}
