"""Internal, fixed C-Eval send-bound profile. Never accept client proof or retries.

Bound unit: client HTTP sends per Run, including failed attempts. The initial
matrix sum does not cap a lifetime of separately authorized superseding Runs.
An independently retrying remote gateway is outside this client-send boundary.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib import metadata
import json
import platform
from pathlib import Path
import sys
from urllib.parse import urlsplit

PROFILE_ID = "motte-ceval-oc042-bounded@1"
RETRY_POLICY = {"runner": 0, "provider_transport": 1, "operator": 0}
_PROFILE_FILE = Path(__file__).with_name("bounded-profile.json")
_BRIDGE_FILES = ("execution.py", "bounded-profile.json", "upstream.py", "entry.py", "config.py")
_FIXED = {
    "profile_id": PROFILE_ID,
    "model": "RuntimeOpenAI",
    "model_count": 1,
    "num_gpus": 0,
    "num_procs": 1,
    "partitioner": "NumWorkerPartitioner",
    "num_worker": 1,
    "num_split": 1,
    "strategy": "heuristic",
    "runner": "LocalRunner",
    "max_num_workers": 1,
    "task": "OpenICLInferTask",
    "inferencer": "GenInferencer",
    "batch_size": 1,
    "retriever": "ZeroRetriever",
    "n": 1,
    "adapter_retries": 0,
    "redirects": False,
    "trust_env": False,
    "timeout": [10, 60],
    "mode": "infer",
    "reuse": False,
    "retry": RETRY_POLICY,
    "bound_scope": "per-run-client-http-sends",
}


def freeze_execution_profile(profile_id):
    if profile_id != PROFILE_ID:
        raise ValueError("EXPERIMENT_BUDGET_UNPROVABLE: unsupported C-Eval execution profile")
    return {
        **deepcopy(_FIXED),
        "pinned_identity": json.loads(_PROFILE_FILE.read_text()),
        "bridge_sha256": {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in _BRIDGE_FILES
        },
    }


def profile_retry_policy(profile_id, retry_policy=None):
    freeze_execution_profile(profile_id)
    if retry_policy is not None and (
        not isinstance(retry_policy, dict)
        or set(retry_policy) - set(RETRY_POLICY)
        or any(
            type(value) is not int or value != RETRY_POLICY[key]
            for key, value in retry_policy.items()
        )
    ):
        raise ValueError("EXECUTION_PROFILE_INVALID: retry declarations differ from fixed profile")
    return dict(RETRY_POLICY)


def validate_execution_config(config):
    profile = config.get("execution_profile")
    if not isinstance(profile, dict) or json.dumps(profile, sort_keys=True) != json.dumps(
        freeze_execution_profile(profile.get("profile_id")), sort_keys=True
    ):
        raise ValueError("EXECUTION_PROFILE_INVALID: implementation or fixed conditions differ")
    if config.get("retry") != RETRY_POLICY or any(
        type(value) is not int for value in config["retry"].values()
    ):
        raise ValueError("EXECUTION_PROFILE_INVALID: retry policy differs")
    forbidden = {
        "models",
        "model_dataset_combinations",
        "infer",
        "runner",
        "generation_kwargs",
        "fallback",
        "cli_args",
        "reuse",
        "partitioner",
    }
    if forbidden.intersection(config):
        raise ValueError("EXECUTION_PROFILE_INVALID: custom execution overrides are forbidden")
    cases = config.get("cases")
    if (
        config.get("benchmark_id") != "ceval"
        or not isinstance(cases, list)
        or not cases
        or any(
            not isinstance(case, dict)
            or not isinstance(case.get("prompt"), str)
            or not case.get("case_id")
            for case in cases
        )
        or len({case["case_id"] for case in cases}) != len(cases)
    ):
        raise ValueError("EXECUTION_PROFILE_INVALID: unique frozen C-Eval prompts required")
    identity = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                profile,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    if config.get("environment_digest") != identity:
        raise ValueError("EXECUTION_PROFILE_INVALID: environment identity differs")
    unhashed = {key: value for key, value in config.items() if key != "config_hash"}
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                unhashed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    if config.get("config_hash") != digest:
        raise ValueError("EXECUTION_PROFILE_INVALID: frozen config hash differs")
    return profile


def verify_installed_profile(profile):
    """Run inside the chosen runner interpreter before exporting or launching.

    Versions alone do not attest code. Also check trusted primary-source bytes of
    the complete selected fanout and transport path, plus the frozen bridge bytes.
    """
    if json.dumps(profile, sort_keys=True) != json.dumps(
        freeze_execution_profile(profile.get("profile_id")), sort_keys=True
    ):
        raise ValueError("EXECUTION_PROFILE_INVALID: runner bridge bytes differ")
    if sys.version_info[:3] != (3, 10, 20):
        raise ValueError("EXECUTION_PROFILE_INVALID: runner requires Python 3.10.20")
    identity = profile["pinned_identity"]
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise ValueError("EXECUTION_PROFILE_INVALID: runner requires verified Linux x86_64")
    for distribution, version in identity["dependencies"].items():
        if metadata.version(distribution) != version:
            raise ValueError(f"EXECUTION_PROFILE_INVALID: dependency mismatch: {distribution}")
    for distribution, files in identity["source_sha256"].items():
        installed = metadata.distribution(distribution)
        for relative, expected in files.items():
            path = installed.locate_file(relative)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(
                    f"EXECUTION_PROFILE_INVALID: installed source mismatch: {relative}"
                )


class TransportBudgetExhausted(RuntimeError):
    """Terminal exhaustion, deliberately not a retryable Requests exception."""


def bounded_generate(model, prompt, max_out_len, temperature):
    """Pinned OpenAI preprocessing and payload, with one consumed send per loop.

    Do not delegate to upstream _generate: 0.4.2 fails to count several error
    branches. A private Session prevents shared model threads from changing each
    other's adapters, cookies or hooks. No SDK, proxy, fallback, or redirects.
    """
    import requests

    attempts = model.retry
    if type(attempts) is not int or attempts < 1:
        raise ValueError("EXECUTION_PROFILE_INVALID: positive integer attempts required")
    endpoint = urlsplit(model.url) if isinstance(model.url, str) else None
    if (
        endpoint is None
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or not endpoint.hostname
        or endpoint.scheme not in {"http", "https"}
        or (
            endpoint.scheme == "http" and endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
        )
        or model.proxy_url is not None
        or len(model.keys) != 1
        or model.orgs
        or model.logprobs
        or model.top_logprobs
        or set(model.extra_body or {}) - {"top_p"}
    ):
        raise ValueError("EXECUTION_PROFILE_INVALID: unsupported endpoint/model transport options")
    messages, output_tokens = model._preprocess_messages(
        prompt,
        max_out_len,
        model.max_seq_len,
        model.mode,
        model.get_token_len,
    )
    payload = {
        "model": model.path,
        "messages": messages,
        "n": 1,
        "logprobs": False,
        "top_logprobs": None,
        "stop": None,
        "temperature": temperature,
        **(model.extra_body or {}),
    }
    output_key = (
        "max_completion_tokens"
        if any(name in model.path for name in ("o1", "o3"))
        else "max_tokens"
    )
    payload[output_key] = output_tokens
    headers = {
        "Authorization": "Bearer " + model.keys[0],
        "api-key": model.keys[0],
        "content-type": "application/json",
    }
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
        for _attempt in range(attempts):
            model.wait()
            try:
                with session.post(
                    model.url,
                    headers=headers,
                    json=payload,
                    allow_redirects=False,
                    timeout=(10, 60),
                ) as response:
                    if response.status_code != 200:
                        continue
                    body = response.json()
                    content = body["choices"][0]["message"]["content"]
                    if isinstance(content, str):
                        return content.strip()
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
                # Every error uses this iteration; no error path can reset it.
                continue
    raise TransportBudgetExhausted(f"model transport exhausted {attempts} attempts")
