import pytest
from motte_sandbox.policy import SandboxPolicy
def test_network_defaults_none(): assert SandboxPolicy().network == "none"
def test_policy_rejects_privileged_and_escape():
 with pytest.raises(ValueError): SandboxPolicy(privileged=True)
 with pytest.raises(ValueError): SandboxPolicy(workspace="../x")
