class InspectHarness:
    def run(self, *args, **kwargs):
        return {"status": "dry_run", "args": args}
