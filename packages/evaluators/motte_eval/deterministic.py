import re
from .base import EvaluationResult


def exact_match(a, b):
    return EvaluationResult(a == b, 1 if a == b else 0)


def regex_match(a, p):
    ok = re.search(p, a) is not None
    return EvaluationResult(ok, 1 if ok else 0)
