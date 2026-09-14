import pytest
from motte_provider.capabilities import validate_parameters, UnsupportedParameterError
from motte_provider.openai_chat import normalize_response
from motte_provider.catalog import ModelCatalog
from motte_provider.pricing import PriceTable, estimate_cost


def test_openai_response_normalizes_to_common_shape():
    x = normalize_response(
        {
            "id": "x",
            "model": "m",
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }
    )
    assert x.content == "hi" and x.usage["prompt_tokens"] == 2


def test_unsupported_parameters_fail_strictly():
    with pytest.raises(UnsupportedParameterError):
        validate_parameters({"temperature": 2}, {"temperature": (0, 1)})


def test_catalog_lookup():
    c = ModelCatalog()
    c.register({"id": "m", "provider": "p"})
    assert c.get("m")["provider"] == "p"


def test_pricing_estimate():
    p = PriceTable(version="v1", input_per_million=1, output_per_million=2)
    assert estimate_cost(p, {"prompt_tokens": 1000, "completion_tokens": 2000}) == 0.005
