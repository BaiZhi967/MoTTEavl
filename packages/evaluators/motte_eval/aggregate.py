def aggregate(values): return {"pass":sum(v is not None and v != "unsupported" for v in values),"missing":sum(v is None for v in values),"unsupported":sum(v=="unsupported" for v in values)}
def pass_at_k(results,k): return sum(1 for x in results if x)/len(results) if results else 0
