import asyncio
from motte_sdk.executor import RunExecutor
def test_replay_executor_is_idempotent():
 e=RunExecutor(); assert e.run("r", ["a"]) == e.run("r", ["a"])
