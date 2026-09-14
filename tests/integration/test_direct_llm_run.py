import pytest
from motte_sdk.executor import RunExecutor
@pytest.mark.replay
def test_replay_run_is_reproducible():
    e=RunExecutor(); a=e.run('replay-1',['case-1']); b=e.run('replay-1',['case-1']); assert a==b and a['status']=='completed'
