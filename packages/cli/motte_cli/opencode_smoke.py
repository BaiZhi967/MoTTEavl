"""Opt-in, bounded Space Bunny Free coding-agent smoke; never a benchmark run.

Dry plan: python -m motte_cli.opencode_smoke
Live:     python -m motte_cli.opencode_smoke --live --confirm-free
The operator must first verify current free availability in the OpenCode console.
Defaults to OPENCODE_GO_API_KEY; --credentials opts into an existing saved profile.
Credentials are resolved only after explicit live selection; never saved here.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import threading
import time
from typing import Callable

from motte_agent.budget import ExecutionBudget
from motte_agent.builtin_react import BuiltinReActRuntime
from motte_provider.base import ProviderCallError
from motte_provider.opencode_go import GO_BASE_URL, SPACE_BUNNY_MODEL
from motte_provider.openai_compatible import OpenAICompatibleProvider
from motte_provider.transport import HTTPTransport, _bounded_urlopen

CASES = (
    {"id": "fix-add", "source": "def add(a, b):\n    return a - b\n",
     "instruction": "Fix add(a, b) to return a + b.",
     "expected": "def add(a, b):\n    return a + b\n"},
    {"id": "fix-range", "source": "def first_n(n):\n    return list(range(n + 1))\n",
     "instruction": "Fix first_n(n) to return list(range(n)).",
     "expected": "def first_n(n):\n    return list(range(n))\n"},
    {"id": "fix-default", "source": "def default(value):\n    return value or 10\n",
     "instruction": "Fix default(value) to return 10 if value is None else value.",
     "expected": "def default(value):\n    return 10 if value is None else value\n"},
)
MAX_REQUESTS = 10
MAX_OUTPUT_TOKENS = 512
TOTAL_TIMEOUT = 180.0
CALL_TIMEOUT = 20.0


def smoke_plan() -> dict:
    return {"status": "dry_run", "model": SPACE_BUNNY_MODEL, "base_url": GO_BASE_URL,
            "cases": [case["id"] for case in CASES], "max_http_requests": MAX_REQUESTS,
            "max_retries": 0, "max_steps_per_case": 3, "max_output_tokens": MAX_OUTPUT_TOKENS,
            "per_call_timeout_sec": CALL_TIMEOUT, "total_timeout_sec": TOTAL_TIMEOUT,
            "concurrency": 1, "paid_fallback": False, "follow_redirects": False,
            "free_status": "limited time; operator must verify before live execution"}


class BoundedSender:
    """Count at the actual HTTP-send boundary, including any accidental retries."""

    def __init__(self, *, opener: Callable | None = None, clock: Callable = time.monotonic):
        self.requests = 0
        self._opener = opener or _bounded_urlopen
        self._clock = clock
        self.deadline = clock() + TOTAL_TIMEOUT
        self._lock = threading.Lock()

    def __call__(self, request, *, timeout):
        payload = json.loads(request.data)
        if (request.full_url != GO_BASE_URL + "/chat/completions"
                or request.get_method() != "POST"
                or payload.get("model") != SPACE_BUNNY_MODEL
                or type(payload.get("max_tokens")) is not int
                or not 1 <= payload["max_tokens"] <= MAX_OUTPUT_TOKENS):
            raise ValueError("quick smoke endpoint/model/output limit is pinned")
        with self._lock:
            remaining = self.deadline - self._clock()
            if self.requests >= MAX_REQUESTS:
                raise ValueError("quick smoke request budget exhausted")
            if remaining <= 0:
                raise ValueError("quick smoke time budget exhausted")
            self.requests += 1
        return self._opener(request, timeout=min(timeout, CALL_TIMEOUT, remaining))


def _same_code(actual: str, expected: str) -> bool:
    # Compare syntax without ever executing model-produced code.
    try:
        return ast.dump(ast.parse(actual)) == ast.dump(ast.parse(expected))
    except (SyntaxError, ValueError):
        return False


def run_quick_smoke(*, api_key: str, opener: Callable | None = None) -> dict:
    if not api_key or not api_key.strip():
        raise ValueError("OPENCODE_GO_API_KEY is required")
    sender = BoundedSender(opener=opener)
    transport = HTTPTransport(GO_BASE_URL, api_key, timeout=CALL_TIMEOUT, max_retries=0,
                              follow_redirects=False, opener=sender)
    provider = OpenAICompatibleProvider(transport, SPACE_BUNNY_MODEL,
                                        max_output_tokens=MAX_OUTPUT_TOKENS,
                                        identity_policy="require_match")
    results = []
    for case in CASES:
        files = {"solution.py": case["source"]}
        actions = []

        def read_file(args):
            if args["path"] != "solution.py":
                raise ValueError("only solution.py is available")
            actions.append("read_file")
            return files["solution.py"]

        def write_file(args):
            if args["path"] != "solution.py" or len(args["content"]) > 8192:
                raise ValueError("only a bounded solution.py update is allowed")
            files["solution.py"] = args["content"]
            actions.append("write_file")
            return "solution.py updated"

        runtime = BuiltinReActRuntime(
            provider.complete, {"read_file": read_file, "write_file": write_file},
            model=SPACE_BUNNY_MODEL, mode="native-tool",
            budget=ExecutionBudget(max_steps=3, max_tool_calls=3,
                                   per_call_timeout_sec=CALL_TIMEOUT,
                                   max_output_tokens=MAX_OUTPUT_TOKENS),
        )
        runtime.begin()
        try:
            result = runtime.send(
                "Read solution.py using read_file, then use write_file to fix this tiny Python "
                "function. " + case["instruction"] +
                " Keep the function name and parameters. Do not add extra code or docstrings. "
                "Finish with a short explanation after writing the file.",
                deadline=sender.deadline,
            )
            passed = (result["termination_reason"] == "final_answer"
                      and "read_file" in actions and "write_file" in actions
                      and _same_code(files["solution.py"], case["expected"]))
            results.append({"id": case["id"], "passed": passed,
                            "termination_reason": result["termination_reason"],
                            "steps": result["steps"], "tools": list(actions)})
        except (ProviderCallError, ValueError) as error:
            # Provider error strings/bodies may echo credentials; only report classification.
            results.append({"id": case["id"], "passed": False,
                            "error_class": getattr(error, "error_class", "guard_rejected")})
        finally:
            runtime.close()
        if not results[-1]["passed"]:
            break  # fail closed, including timeout: no subsequent concurrent/background sends
    return {**smoke_plan(), "status": "passed" if len(results) == 3 and all(
            case["passed"] for case in results) else "failed",
            "cases": results, "http_requests": sender.requests,
            "execution_mode": "live" if opener is None else "mock"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly send the bounded coding smoke")
    parser.add_argument("--confirm-free", action="store_true",
                        help="I verified Space Bunny Free is still free in my OpenCode console")
    parser.add_argument("--credentials", help="explicit saved credential profile; no env fallback")
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps(smoke_plan(), ensure_ascii=False, indent=2))
        return 0
    if not args.confirm_free:
        print("Check current free availability, then add --confirm-free. No requests sent.",
              file=sys.stderr)
        return 2
    if args.credentials is not None:
        from motte_provider.credentials import resolve_api_key

        try:
            api_key = resolve_api_key(args.credentials, env_name=None)
        except (OSError, ValueError):
            print("Unable to read the selected credential profile. No requests sent.",
                  file=sys.stderr)
            return 2
    else:
        api_key = os.environ.get("OPENCODE_GO_API_KEY")
    if not api_key or not api_key.strip():
        print("Selected credential profile or OPENCODE_GO_API_KEY is missing. "
              "Configure it yourself securely; do not paste a key into chat. No requests sent.",
              file=sys.stderr)
        return 2
    report = run_quick_smoke(api_key=api_key)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
