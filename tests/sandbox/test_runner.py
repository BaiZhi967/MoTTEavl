from motte_sandbox.runner import SandboxRunner
def test_dry_run_does_not_execute(): assert SandboxRunner(dry_run=True).run(["echo","ok"])["status"] == "dry_run"
