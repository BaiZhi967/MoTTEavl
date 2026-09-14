import pytest
from pydantic import ValidationError
from motte_contracts.model import ModelProfile, ParameterProfile
from motte_contracts.scenario import ScenarioSpec


def test_model_profile_requires_declared_modalities_and_limits():
    with pytest.raises(ValidationError):
        ModelProfile(id="m", provider="p", capabilities={"input_modalities": []})


def test_scenario_rejects_invalid_temperature():
    with pytest.raises(ValidationError):
        ParameterProfile(temperature=3)


def test_scenario_has_strict_parameter_policy():
    scenario = ScenarioSpec(id="s", version=1, mode="direct", dataset="d", model="m")
    assert scenario.parameter_policy == "strict"
