class RunExecutor:
 def __init__(self): self._done={}
 def run(self,run_id,cases):
  if run_id not in self._done: self._done[run_id]={"run_id":run_id,"status":"completed","cases":list(cases)}
  return self._done[run_id].copy()
