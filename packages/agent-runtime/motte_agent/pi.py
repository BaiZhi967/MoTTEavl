from .runtime import AgentRuntime


class PiAgentRuntime(AgentRuntime):
    def run(self, prompt):
        return prompt
