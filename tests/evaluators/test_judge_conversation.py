"""Independent judge calls carry safe conversation identity at the Go boundary."""
from __future__ import annotations

import json
from urllib.error import HTTPError
from uuid import UUID

import pytest

from motte_eval.judge import build_judge_input, build_judge_request, build_pairwise_input
from motte_provider.openai_compatible import OpenAICompatibleProvider
from motte_provider.opencode_go import GO_BASE_URL, SPACE_BUNNY_MODEL
from motte_provider.transport import HTTPTransport
from tests.evaluators.test_judge_execution import candidate, observation, pair_spec, spec
from tests.provider.test_openai_compatible import FakeResponse, chat_body


@pytest.fixture(params=["single", "pairwise"])
def coding_judge(request):
    parameters = {"max_output_tokens": 128}
    if request.param == "single":
        judge = spec(model=SPACE_BUNNY_MODEL, parameters=parameters,
                     input_selector={"fields": ["final_output"]})
        bundle = build_judge_input(judge, observation(
            final_output="def add(a, b):\n    return a + b\n",
        ))
    else:
        judge = pair_spec(model=SPACE_BUNNY_MODEL, parameters=parameters)
        bundle = build_pairwise_input(
            task_ref="fix-add",
            candidate_a=candidate("correct", "def add(a, b): return a + b", "10"),
            candidate_b=candidate("incorrect", "def add(a, b): return a - b", "20"),
            presentation_order=["correct", "incorrect"],
        )
    return judge, bundle


def test_each_judge_request_gets_an_independent_safe_conversation(coding_judge):
    judge, bundle = coding_judge
    frozen_spec = judge.model_dump(mode="json")
    frozen_input = bundle.model_dump(mode="json")

    first = build_judge_request(judge, bundle)
    second = build_judge_request(judge, bundle)

    first_id = first.metadata.get("session_id")
    second_id = second.metadata.get("session_id")
    assert isinstance(first_id, str) and len(first_id) == 32
    assert isinstance(second_id, str) and len(second_id) == 32
    assert UUID(hex=first_id).version == UUID(hex=second_id).version == 4
    assert first_id != second_id
    first_payload = first.model_dump(mode="json")
    second_payload = second.model_dump(mode="json")
    first_payload["metadata"].pop("session_id")
    second_payload["metadata"].pop("session_id")
    assert first_payload == second_payload
    assert first.metadata["purpose"] == "judge"
    assert first.tools == []
    assert judge.model_dump(mode="json") == frozen_spec
    assert bundle.model_dump(mode="json") == frozen_input


def test_go_judge_request_is_accepted_and_retry_keeps_conversation(coding_judge):
    judge, bundle = coding_judge
    request = build_judge_request(judge, bundle)
    sent = []
    secret = "sk-synthetic-judge-key-never-report"

    def opener(wire_request, *, timeout):
        sent.append({"session": wire_request.get_header("X-opencode-session"),
                     "user_agent": wire_request.get_header("User-agent"),
                     "url": wire_request.full_url, "body": json.loads(wire_request.data)})
        if len(sent) == 1:
            raise HTTPError(wire_request.full_url, 503, "busy", {}, None)
        body = chat_body("Code review complete")
        body["model"] = SPACE_BUNNY_MODEL
        return FakeResponse(body)

    provider = OpenAICompatibleProvider(
        HTTPTransport(GO_BASE_URL, secret, opener=opener, max_retries=1,
                      sleep=lambda _: None,
                      default_headers={"Idempotency-Key": "synthetic-judge-retry"}),
        SPACE_BUNNY_MODEL, max_output_tokens=128, identity_policy="require_match",
    )
    envelope = provider.complete(request)

    assert envelope["content"] == "Code review complete"
    assert envelope["metering"]["attempts"] == 2
    assert len(sent) == 2
    assert {item["session"] for item in sent} == {
        request.metadata["session_id"],
    }
    assert all(item["user_agent"] == "MoTTEavl/0.1.0" for item in sent)
    assert all(item["url"] == GO_BASE_URL + "/chat/completions" for item in sent)
    assert all("metadata" not in item["body"] for item in sent)
    assert secret not in request.metadata["session_id"]
    assert secret not in json.dumps(provider.calls)
