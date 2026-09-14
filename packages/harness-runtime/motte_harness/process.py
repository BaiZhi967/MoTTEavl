class ProcessRunner:
    async def run(self, command, timeout=None): return {"status":"dry_run","command":list(command)}
