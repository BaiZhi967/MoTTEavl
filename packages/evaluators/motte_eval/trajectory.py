from .base import EvaluationResult
def tool_order(actual, expected):
    ok=list(actual)==list(expected); return EvaluationResult(ok,1 if ok else 0,{"expected":expected,"actual":actual})
