from motte_contracts.events import TraceEvent
class TraceSink:
 def __init__(self,run_id): self.run_id=run_id; self.seq=0; self.events=[]
 def emit(self,type,payload=None):
  self.seq+=1; self.events.append(TraceEvent(run_id=self.run_id,seq=self.seq,span_id=f"span-{self.seq}",type=type,payload=payload or {})); return self.seq
