from motte_eval.deterministic import exact_match, regex_match
from motte_eval.aggregate import aggregate, pass_at_k
from motte_eval.gates import RegressionGate
def test_exact_regex(): assert exact_match("a","a").passed; assert regex_match("abc","b").passed
def test_aggregate_distinguishes_missing(): assert aggregate([1,None,"unsupported"])=={"pass":1,"missing":1,"unsupported":1}
def test_pass_k(): assert pass_at_k([True,False],1)==.5
def test_gate_direction(): assert RegressionGate(0.8,"gte").check(.9)
