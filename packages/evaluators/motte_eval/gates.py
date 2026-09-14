class RegressionGate:
    def __init__(self,threshold,direction="gte"): self.threshold=threshold; self.direction=direction
    def check(self,value): return value>=self.threshold if self.direction=="gte" else value<=self.threshold
