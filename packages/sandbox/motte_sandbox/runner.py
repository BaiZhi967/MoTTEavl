class SandboxRunner:
 def __init__(self, policy=None, dry_run=True): self.policy=policy; self.dry_run=dry_run
 def run(self, command, **kwargs):
  if self.dry_run: return {"status":"dry_run","command":list(command)}
  raise RuntimeError("real sandbox execution is disabled")
