import json

import pytest

from motte_sdk.context_preflight import (
    MESSAGE_STRUCTURE_TOKENS,
    METHOD_VERSION,
    REQUEST_STRUCTURE_TOKENS,
    estimate_context_preflight,
)
from motte_sdk.resolve import ManifestResolutionError, prepare_run
from motte_storage.resource_store import InMemoryResourceStore


def test_utf8_bytes_are_used_as_the_content_token_upper_bound():
    report = estimate_context_preflight(
        {"zh": {"prompt": "你好"}}, context_window=4096, output_tokens_reserved=256
    )
    estimate = report["per_case"]["zh"]

    assert report["method"] == METHOD_VERSION
    assert estimate["utf8_bytes"] == 6
    assert estimate["structure_tokens"] == REQUEST_STRUCTURE_TOKENS + MESSAGE_STRUCTURE_TOKENS
    assert estimate["input_tokens_upper_bound"] == 6 + estimate["structure_tokens"]
    assert estimate["total_tokens_upper_bound"] == estimate["input_tokens_upper_bound"] + 256
    assert "你好" not in json.dumps(report, ensure_ascii=False)


def test_messages_multiple_cases_and_nearest_rank_summary():
    report = estimate_context_preflight(
        {
            "short": {"prompt": "a"},
            "medium": {"prompt": "abcdefghij"},
            "chat": {"messages": [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "question"},
            ]},
        },
        context_window=8192,
        output_tokens_reserved=32,
    )
    per_case = report["per_case"]
    totals = sorted(item["total_tokens_upper_bound"] for item in per_case.values())

    assert per_case["chat"]["source"] == "messages"
    assert per_case["chat"]["message_count"] == 2
    assert report["stats"]["case_count"] == 3
    assert report["stats"]["total_tokens_upper_bound"] == {
        "max": totals[2], "p50": totals[1], "p95": totals[2]
    }


@pytest.mark.parametrize(
    "context_window,output_tokens_reserved,message",
    [(True, 1, "context_window"), (1024, -1, "output_tokens_reserved")],
)
def test_estimator_rejects_invalid_limits(context_window, output_tokens_reserved, message):
    with pytest.raises(ValueError, match=message):
        estimate_context_preflight(
            {}, context_window=context_window, output_tokens_reserved=output_tokens_reserved
        )


def test_output_reserve_is_added_to_every_case():
    cases = {"a": {"prompt": "same"}, "b": {"prompt": "same"}}
    small = estimate_context_preflight(
        cases, context_window=4096, output_tokens_reserved=16
    )
    large = estimate_context_preflight(
        cases, context_window=4096, output_tokens_reserved=128
    )

    for case_id in cases:
        assert large["per_case"][case_id]["total_tokens_upper_bound"] - \
            small["per_case"][case_id]["total_tokens_upper_bound"] == 112


def test_prepare_run_skips_profiles_without_a_context_window():
    resources = InMemoryResourceStore()
    resources.providers.put({
        "name": "local", "kind": "openai_compatible",
        "base_url": "https://local.test/v1", "model": "unused",
    })
    resources.models.put({
        "id": "model", "provider": "local", "model": "model-1",
        "capabilities": {}, "max_output_tokens": 64,
    })

    resolved, case_ids = prepare_run(
        "direct-llm@1",
        {"model": "model", "cases": {"case-1": {"prompt": "x" * 10000}}},
        ["case-1"],
        resources,
    )

    assert case_ids == ["case-1"]
    assert "context_preflight" not in resolved.get("budget", {})
    assert "context_window" not in resolved["resource_snapshots"]["model_profile"]


def test_prepare_run_does_not_trust_inline_provider_context_metadata():
    resolved, _ = prepare_run(
        "direct-llm@1",
        {
            "provider": {
                "kind": "openai_compatible", "base_url": "https://local.test/v1",
                "model": "inline", "context_window": 1,
                "parameters": {"max_output_tokens": 1},
            },
            "cases": {"case-1": {"prompt": "far longer than one token"}},
        },
        ["case-1"],
        InMemoryResourceStore(),
    )

    assert "context_preflight" not in resolved.get("budget", {})
    assert "model_profile" not in resolved.get("resource_snapshots", {})


def test_client_cannot_claim_that_context_preflight_already_ran():
    with pytest.raises(ManifestResolutionError) as error:
        prepare_run(
            "direct-llm@1",
            {
                "provider": {
                    "kind": "openai_compatible", "base_url": "https://local.test/v1",
                    "model": "inline", "parameters": {"max_output_tokens": 1},
                },
                "cases": {"case-1": {"prompt": "short"}},
                "budget": {"context_preflight": {"method": "claimed-by-client"}},
            },
            ["case-1"],
            InMemoryResourceStore(),
        )

    assert error.value.code == "SNAPSHOT_RESERVED"
