from .runtime import AgentRuntime
class BuiltinReActRuntime(AgentRuntime):
 def __init__(self, tools=None, max_steps=8): self.tools=tools or {}; self.max_steps=max_steps
 def run(self,prompt):
  if self.max_steps<=0: return "budget_exceeded"
  return prompt
