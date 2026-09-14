# MoTTEavl Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a long-lived, single-user evaluation platform that runs and compares direct LLM calls, Pi agents with skills and Docker sandboxes, and Claude/Codex CLI harnesses through one versioned evaluation protocol.

**Architecture:** A Python control and execution plane owns the canonical contracts, Provider adapters, runtimes, evaluators, storage, API, and CLI. A TypeScript Pi bridge and React/Vite Web application consume versioned JSON/JSONL contracts; every executor emits the same Run, TraceEvent, Artifact, and Score evidence model.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic v2, asyncio, Celery, Redis, PostgreSQL, Docker, OpenTelemetry, uv workspace, TypeScript, pnpm, React, Vite, `@mariozechner/pi-agent-core`, `@mariozechner/pi-ai`, Claude CLI, Codex app-server/exec, Inspect AI adapter.

**Spec:** `docs/superpowers/specs/2026-09-14-llm-agent-harness-evaluation-platform-design.md`

## Global Constraints

- Product scope is single-machine and single-user; do not add multi-user, multi-tenant, RBAC, or cross-user sharing.
- Delivery is Web + CLI over one Python SDK and one versioned contract layer.
- Provider protocols are `openai_chat`, `openai_responses`, `anthropic_messages`, and `openai_compatible`.
- Public API starts at `/api/v1`; breaking changes require a new API version and migration notes.
- Every Run stores Scenario, dataset, model profile, parameter, Agent/Skill/Harness, Git revision, image digest, seed, and budget in a resolved manifest.
- Formal evaluation defaults to strict parameter and capability validation; unsupported requirements produce `unsupported` before execution.
- Docker execution uses an isolated workspace, CPU/memory/PID/disk/TTL limits, non-root where available, read-only rootfs where possible, and deny-by-default network policy.
- Linux/WSL2 is the first-class host; Windows Docker Desktop is supported with capability differences recorded in the resolved manifest.
- Secrets are referenced through environment variables or the local keyring and never returned by API or stored in traces.
- The first Web UI language is Simplified Chinese; protocol fields, CLI JSON, and error codes remain stable English identifiers.
- Public protocols must not depend on LiteLLM, Pi, Claude, Codex, Inspect, a database, or a UI framework.
- Every task below ends with a focused test command and a commit.

---

## Milestone Mapping

The task order is dependency-first, with a vertical release boundary after Task 5. It does not defer the first end-to-end Run until the final tasks.

| Release boundary | Tasks | Required behavior |
| --- | --- | --- |
| Foundation slice | 1–5 | Two entry points create and trace a replay-backed Run; CI is active from Task 1 |
| Provider evaluation | 6 | Chat, Responses, Anthropic, and compatible adapters with model profile and price evidence |
| Agent evaluation | 7–8 | BuiltinReAct, Pi, Skill, Docker, and policy-separated tools |
| Harness evaluation | 9 | Claude/Codex bidirectional channels and Inspect adapter |
| Evaluation product | 10–11 | Evaluators, regression gates, complete API/CLI/Web |
| Stable release | 12 | Integration, backup/restore, package, compatibility, and release checks |

---

## File Map Before Implementation

The following paths are created by the tasks below. A file has one primary responsibility.

```text
apps/api/app/main.py                         # FastAPI application factory
apps/worker/motte_worker/tasks.py            # Celery task entrypoints
apps/web/src/                              # React/Vite UI
packages/contracts/motte_contracts/          # Pydantic public contracts
packages/sdk-python/motte_sdk/               # User-facing Python SDK
packages/cli/motte_cli/                      # CLI commands and renderers
packages/provider-runtime/motte_provider/    # Provider protocol adapters
packages/agent-runtime/motte_agent/         # AgentRuntime and Pi client
packages/harness-runtime/motte_harness/      # Harness adapters and process IO
packages/skill-runtime/motte_skill/          # Skill manifests and registries
packages/sandbox/motte_sandbox/              # Docker sandbox implementation
packages/evaluators/motte_eval/              # Evaluators and aggregation
packages/trace/motte_trace/                  # Event sink, redaction, replay
packages/storage/motte_storage/              # Repositories and artifacts
bridges/pi/src/index.ts                      # Node Pi bridge process
migrations/                                  # Alembic migrations
tests/                                       # Contract, unit, integration, fixtures
infra/docker-compose.yml                     # Local services
```

## Task 1: Workspace and Local Service Foundation

**Files:**
- Create: `pyproject.toml`, `uv.lock`, `packages/*/pyproject.toml`
- Create: `package.json`, `pnpm-workspace.yaml`, `apps/web/package.json`, `bridges/pi/package.json`
- Create: `.env.example`, `.gitignore`, `README.md`, `infra/docker-compose.yml`
- Create: `.github/workflows/ci.yml`
- Create: `tests/test_workspace_health.py`

**Interfaces:**
- Produces Python importable packages for contracts, SDK, runtime, storage, trace, evaluators, CLI, API, and worker.
- Produces Node packages for the Web app and Pi bridge.
- Produces service names `postgres`, `redis`, `otel-collector` and a mounted artifact directory.

- [ ] **Step 1: Write the failing health test**

```python
def test_workspace_packages_import():
    import motte_contracts
    import motte_sdk
    import motte_provider
    import motte_agent
    import motte_harness
    import motte_skill
    import motte_sandbox
    import motte_eval
    import motte_trace
    import motte_storage

    assert motte_contracts.__version__
    assert motte_sdk.__version__
    assert motte_provider.__version__
    assert motte_agent.__version__
    assert motte_harness.__version__
    assert motte_skill.__version__
    assert motte_sandbox.__version__
    assert motte_eval.__version__
    assert motte_trace.__version__
    assert motte_storage.__version__
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_workspace_health.py -q`  
Expected: FAIL because the packages do not exist.

- [ ] **Step 3: Add the workspace metadata and package stubs**

Use uv workspace members for every Python package, expose a `__version__` constant from each package, and use pnpm workspace members for Web and bridge. Pin compatible Python and Node ranges in package metadata.

- [ ] **Step 4: Add Docker Compose services and environment examples**

Expose PostgreSQL only on localhost, Redis only on localhost, and mount `./var/artifacts` to the worker. Define `DATABASE_URL`, `REDIS_URL`, `ARTIFACT_ROOT`, `OTEL_EXPORTER_OTLP_ENDPOINT`, and provider credential references in `.env.example` without secret values.

- [ ] **Step 5: Add quality configuration, README, and baseline CI**

Configure Ruff and mypy in the root `pyproject.toml`, configure pytest markers for `replay` and `live`, and write a README containing the project purpose, architecture/spec links, local setup, and the first health command. Add `.github/workflows/ci.yml` that runs package imports, Ruff, mypy, contract tests, and Compose config on every push and pull request.

- [ ] **Step 6: Run the test and service config validation**

Run: `uv run pytest tests/test_workspace_health.py -q` and `docker compose -f infra/docker-compose.yml config`  
Expected: PASS and valid Compose output.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock package.json pnpm-workspace.yaml apps bridges packages infra .env.example .gitignore .github/workflows/ci.yml README.md tests/test_workspace_health.py
git commit -m "chore: bootstrap motteavl workspaces"
```

## Task 2: Canonical Contracts and Schema Validation

**Files:**
- Create: `packages/contracts/motte_contracts/messages.py`
- Create: `packages/contracts/motte_contracts/model.py`
- Create: `packages/contracts/motte_contracts/scenario.py`
- Create: `packages/contracts/motte_contracts/run.py`
- Create: `packages/contracts/motte_contracts/events.py`
- Create: `packages/contracts/motte_contracts/evidence.py`
- Create: `packages/contracts/motte_contracts/dataset.py`
- Create: `packages/contracts/motte_contracts/errors.py`
- Create: `packages/contracts/motte_contracts/jsonschema.py`
- Create: `tests/contract/test_contract_roundtrip.py`
- Create: `tests/contract/test_scenario_validation.py`
- Create: `tests/contract/test_dataset_validation.py`

**Interfaces:**
- Produces `ModelRequest`, `ModelResponse`, `StreamEvent`, `ModelProfile`, `ReasoningProfile`, `ParameterProfile`, `ScenarioSpec`, `Case`, `Run`, `CaseRun`, `ResolvedManifest`, `TraceEvent`, `Artifact`, `Observation`, and `Score`.
- Produces `dump_json_schema()` for all public contracts.
- Consumes no Provider SDK, framework, database, or application code.

- [ ] **Step 1: Write failing contract round-trip tests**

```python
def test_trace_event_round_trips_with_version_and_parent():
    event = TraceEvent(
        protocol="motte.trace",
        schema_version=1,
        run_id="run-1",
        seq=2,
        span_id="span-2",
        parent_span_id="span-1",
        type="tool_call",
        payload={"name": "read_file", "arguments": {"path": "README.md"}},
    )
    assert TraceEvent.model_validate_json(event.model_dump_json()) == event
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/contract -q`  
Expected: FAIL because contract classes are absent.

- [ ] **Step 3: Implement typed contracts**

Use discriminated unions for message content, tool calls, stream events, evaluator definitions, and run states. Represent model capabilities with `input_modalities`, `output_modalities`, `supports_tools`, `tool_features`, token limits, `ReasoningProfile`, `ParameterProfile`, provenance, and profile hash. Make all collections immutable at the boundary or copied on validation.

- [ ] **Step 4: Implement Scenario and capability validation**

Validate mode-specific references, strict parameter policy, required modalities, context and output bounds, and unsupported reasoning levels before a Run is queued. Return structured errors with JSON pointers and error codes.

- [ ] **Step 5: Implement DatasetVersion JSONL validation**

Define one JSON object per line with unique `case_id`, required `input`, optional `expected` and `metadata`; validate encoding, JSON syntax, duplicate IDs, input shape, expected references, line count, and file hash. Keep `metadata` out of model input unless Scenario explicitly maps it.

- [ ] **Step 6: Implement JSON Schema generation and run tests**

Run: `uv run pytest tests/contract -q`  
Expected: PASS, including round-trip, invalid capability, invalid parameter, and schema generation cases.

- [ ] **Step 7: Commit**

```bash
git add packages/contracts tests/contract
git commit -m "feat: add canonical evaluation contracts"
```

## Task 3: Storage, Migrations, and Artifact Repositories

**Files:**
- Create: `packages/storage/motte_storage/db.py`
- Create: `packages/storage/motte_storage/models.py`
- Create: `packages/storage/motte_storage/repositories.py`
- Create: `packages/storage/motte_storage/artifacts.py`
- Create: `migrations/env.py`
- Create: `migrations/versions/0001_initial.py`
- Create: `tests/storage/test_repositories.py`
- Create: `tests/storage/test_artifacts.py`

**Interfaces:**
- Consumes contracts from Task 2.
- Produces repositories for ProviderConnection, ModelProfile, PriceTable, DatasetVersion, ScenarioVersion, AgentVersion, SkillVersion, HarnessVersion, Run, CaseRun, TraceEvent, Artifact, Score, and Report.
- Produces `ArtifactStore.put/read/sha256/delete` and a PostgreSQL-backed metadata repository.

- [ ] **Step 1: Write failing repository tests**

```python
async def test_run_repository_preserves_resolved_manifest(repository):
    run = await repository.create_run(
        scenario_version="scenario@1",
        resolved_manifest={"model_profile_hash": "abc", "seed": 7},
    )
    loaded = await repository.get_run(run.id)
    assert loaded.resolved_manifest["model_profile_hash"] == "abc"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/storage -q`  
Expected: FAIL because repositories and migrations are absent.

- [ ] **Step 3: Implement SQLAlchemy models and migration**

Use JSONB for canonical payloads and raw provider evidence, unique constraints on versioned resources, append-only TraceEvent rows keyed by `(run_id, seq)`, foreign keys from scores/artifacts to CaseRun or Run, and versioned PriceTable rows with currency, unit, tiers, validity, source URL, and observed time.

- [ ] **Step 4: Implement local artifact store**

Write artifacts under `ARTIFACT_ROOT/runs/<run_id>/<artifact_id>`, calculate SHA-256 while writing, store media type and size, and reject path traversal. Keep the interface replaceable with MinIO.

- [ ] **Step 5: Run migration and tests**

Run: `docker compose -f infra/docker-compose.yml up -d postgres`; `uv run alembic upgrade head`; `uv run pytest tests/storage -q`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/storage migrations tests/storage
git commit -m "feat: add versioned storage and artifacts"
```

## Task 4: Provider Runtime and Model Catalog

**Files:**
- Create: `packages/provider-runtime/motte_provider/adapter.py`
- Create: `packages/provider-runtime/motte_provider/capabilities.py`
- Create: `packages/provider-runtime/motte_provider/errors.py`
- Create: `packages/provider-runtime/motte_provider/openai_chat.py`
- Create: `packages/provider-runtime/motte_provider/openai_responses.py`
- Create: `packages/provider-runtime/motte_provider/anthropic_messages.py`
- Create: `packages/provider-runtime/motte_provider/openai_compatible.py`
- Create: `packages/provider-runtime/motte_provider/catalog.py`
- Create: `packages/provider-runtime/motte_provider/pricing.py`
- Create: `tests/provider/test_request_translation.py`
- Create: `tests/provider/test_stream_normalization.py`
- Create: `tests/provider/fixtures/*.json`

**Interfaces:**
- Consumes `ModelRequest`, `ModelProfile`, and `ParameterProfile`.
- Produces `ModelAdapter.validate/invoke/stream`, normalized `ModelResponse` and `StreamEvent`, capability probes, versioned PriceTable loading/calculation, optional LiteLLM transport backend, and classified `ProviderError`.
- Preserves canonical and provider raw payloads.

- [ ] **Step 1: Write failing translation tests for all protocols**

```python
def test_anthropic_translation_uses_top_level_system_and_max_tokens():
    payload = AnthropicAdapter(profile).translate(request)
    assert payload["system"] == "You are precise."
    assert payload["max_tokens"] == 512
    assert "max_output_tokens" not in payload
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/provider -q`  
Expected: FAIL because adapters are absent.

- [ ] **Step 3: Implement protocol adapters**

Implement translation and parsing for Chat Completions, Responses item/event streams, Anthropic content blocks and tool use, and OpenAI-compatible Chat payloads. Keep `provider_options` explicit and redact secrets before trace persistence.

- [ ] **Step 4: Implement model profile and capability validation**

Support model-level tools, modalities, context window, maximum input/output tokens, reasoning modes/levels/budgets, sampling constraints, structured output, logprobs, seed, cache, and tool-choice features. Add provenance and profile hash. Implement strict/warn/passthrough policies, with strict as the default.

- [ ] **Step 5: Implement catalog synchronization and probes**

Add Provider Models API sync where available, documented profile import, versioned price synchronization, and explicit probes for only capabilities that can be safely verified. Persist a new profile version rather than mutating historical profiles. Expose LiteLLM behind a transport interface so direct SDK/HTTP adapters remain available when LiteLLM cannot represent a provider feature.

- [ ] **Step 6: Run fixture tests**

Run: `uv run pytest tests/provider -q`  
Expected: PASS for translation, stream normalization, usage, tool calls, unsupported parameters, retries, cancellation, and error classes.

- [ ] **Step 7: Commit**

```bash
git add packages/provider-runtime tests/provider
git commit -m "feat: add versioned provider runtime"
```

## Task 5: Trace, Replay, and Run Executor

**Files:**
- Create: `packages/trace/motte_trace/sink.py`
- Create: `packages/trace/motte_trace/redaction.py`
- Create: `packages/trace/motte_trace/replay.py`
- Create: `packages/trace/motte_trace/otel.py`
- Create: `packages/sdk-python/motte_sdk/runner.py`
- Create: `packages/sdk-python/motte_sdk/executor.py`
- Create: `packages/sdk-python/motte_sdk/scheduling.py`
- Create: `apps/api/app/foundation.py`
- Create: `packages/cli/motte_cli/foundation.py`
- Create: `apps/web/src/foundation.tsx`
- Create: `tests/trace/test_redaction.py`
- Create: `tests/trace/test_retention.py`
- Create: `tests/runtime/test_run_executor.py`
- Create: `tests/runtime/test_scheduling.py`
- Create: `tests/api/test_foundation_run.py`
- Create: `tests/cli/test_foundation_run.py`
- Create: `apps/web/tests/foundation.test.tsx`
- Create: `docs/superpowers/specs/adr/2026-09-14-runtime-safety-and-execution-policy.md`

**Interfaces:**
- Consumes runtime contracts and storage repositories from Tasks 2–4.
- Produces `EventSink.emit`, `ReplayProvider`, `RunExecutor.execute`, `CaseScheduler`, idempotent state transitions, and the lifecycle `prepare → execute → collect → score → aggregate → report`. The Worker imports this package only to schedule execution; local CLI execution imports the same executor directly.

- [ ] **Step 1: Write failing redaction and lifecycle tests**

```python
async def test_executor_is_idempotent_for_same_run_id(executor):
    first = await executor.execute(run_id="run-1")
    second = await executor.execute(run_id="run-1")
    assert first.id == second.id
    assert await executor.count_case_runs("run-1") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/trace tests/runtime -q`  
Expected: FAIL because the sink and executor are absent.

- [ ] **Step 3: Implement event sink and redaction**

Assign monotonic sequence numbers per Run, preserve span parentage, redact keys matching credential and secret patterns, and store canonical plus redacted/raw evidence according to configured retention.

- [ ] **Step 4: Implement replay provider and executor state machine**

Implement created, validating, queued, preparing, running, collecting, scoring, aggregating, completed, failed, cancelled, unsupported, and profile_stale states. Synchronous API validation persists successful requests as queued; Worker owns preparing and later transitions. Reject duplicate execution after a terminal state and make retries explicit child attempts. Enter profile_stale only when a queued/preparing profile is expired, revoked, or conflicts with a probe; never mark completed historical Runs stale.

- [ ] **Step 5: Implement CaseScheduler and Provider throttling**

Add `case_concurrency`, `requests_per_minute`, `max_in_flight`, `max_retries`, `backoff_initial_ms`, `backoff_max_ms`, and `jitter_ratio` to limits with defaults 4, profile limit or 60 RPM when unknown, 4, 2, 500 ms, 30 s, and 0.2. Use a ProviderConnection-scoped semaphore and token bucket; retry only 429/retry-after/network transient failures, and persist wait/retry events. Keep missing and unsupported cases distinct from zero scores.

- [ ] **Step 6: Add OpenTelemetry spans and EvidencePolicy**

Create spans for run, model call, agent step, tool call, sandbox command, evaluator, and artifact collection. Attach only safe attributes and use events for detailed payloads. Implement `EvidencePolicy` defaults of 90 days for standard trace/artifacts, 30 days for raw Provider payloads, failed-run retention, pinned-run retention, deletion audit events, and the complete credential/header/environment redaction list from the ADR.

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/trace tests/runtime -q`  
Expected: PASS.

- [ ] **Step 8: Add the foundation vertical slice**

Expose a small local API/CLI/Web path that creates a replay-backed Scenario Run, returns `202 queued` after synchronous validation, streams TraceEvent updates, and renders the final score. Keep this slice on the same `RunExecutor`, repositories, and contracts that later full routers will extend.

- [ ] **Step 9: Run the vertical-slice tests**

Run: `uv run pytest tests/api/test_foundation_run.py tests/cli/test_foundation_run.py -q`; `pnpm --dir apps/web test -- foundation`
Expected: PASS for API, CLI, and Web fixture flows.

- [ ] **Step 10: Commit**

```bash
git add packages/trace packages/sdk-python apps/api/app/foundation.py packages/cli/motte_cli/foundation.py apps/web/src/foundation.tsx apps/web/tests/foundation.test.tsx tests/trace tests/runtime tests/api/test_foundation_run.py tests/cli/test_foundation_run.py docs/superpowers/specs/adr/2026-09-14-runtime-safety-and-execution-policy.md
git commit -m "feat: add trace replay and run executor"
```

## Task 6: Docker Sandbox and Tool Registry

**Files:**
- Create: `packages/sandbox/motte_sandbox/spec.py`
- Create: `packages/sandbox/motte_sandbox/docker.py`
- Create: `packages/sandbox/motte_sandbox/policy.py`
- Create: `packages/skill-runtime/motte_skill/manifest.py`
- Create: `packages/skill-runtime/motte_skill/registry.py`
- Create: `packages/skill-runtime/motte_skill/tools.py`
- Create: `tests/sandbox/test_policy.py`
- Create: `tests/sandbox/test_docker_runner.py`

**Interfaces:**
- Consumes SandboxSpec, Skill manifest, ToolDefinition, and ToolCall contracts.
- Produces Sandbox `create/exec/collect/destroy`, `ToolRegistry`, real/mock/replay/deny dispatch, and manifest validation.

- [ ] **Step 1: Write failing policy tests**

```python
def test_network_is_denied_by_default():
    policy = SandboxPolicy.from_spec(SandboxSpec(image="python:3.12-slim"))
    assert policy.network_mode == "none"

def test_command_allowlist_is_independent_from_network_policy():
    policy = SandboxPolicy.from_spec(
        SandboxSpec(image="python:3.12-slim", command_allowlist=[["python"], ["curl"]])
    )
    assert policy.allows_command(["python", "-c", "print(1)"]) is True
    assert policy.allows_command(["curl", "https://example.com"]) is True
    assert policy.allows_command(["sh", "-c", "rm -rf /"]) is False
    assert policy.network_mode == "none"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/sandbox -q`  
Expected: FAIL because policy and runner are absent.

- [ ] **Step 3: Implement policy and manifest validation**

Require explicit image, workspace, resource limits, network policy, command policy, and tool permissions. Keep network policy (`none`/allowlist/unrestricted), command allow/deny patterns, and resource policy as separate checks. Reject path traversal, privileged containers, host Docker socket mounts, and undeclared environment injection. Record Linux/WSL2 versus Windows Docker Desktop capabilities in the resolved manifest.

- [ ] **Step 4: Implement Docker lifecycle**

Create a per-CaseRun container with an isolated workspace, run commands with timeout and byte limits, collect stdout/stderr and files, then destroy it in a `finally` path.

- [ ] **Step 5: Implement ToolRegistry strategies**

Dispatch real calls through approved handlers, mock calls through fixtures, replay calls through recorded events, and deny all undeclared calls with a structured error event.

- [ ] **Step 6: Run tests with Docker**

Run: `uv run pytest tests/sandbox -q`  
Expected: PASS, including timeout, output truncation, cleanup, deny network, and artifact hash tests.

- [ ] **Step 7: Commit**

```bash
git add packages/sandbox packages/skill-runtime tests/sandbox
git commit -m "feat: add docker sandbox and tool policies"
```

## Task 7: BuiltinReAct and Pi Agent Runtime

**Files:**
- Create: `packages/agent-runtime/motte_agent/protocol.py`
- Create: `packages/agent-runtime/motte_agent/runtime.py`
- Create: `packages/agent-runtime/motte_agent/builtin_react.py`
- Create: `packages/agent-runtime/motte_agent/pi.py`
- Create: `bridges/pi/src/index.ts`
- Create: `bridges/pi/src/protocol.ts`
- Create: `bridges/pi/src/pi-session.ts`
- Create: `tests/runtime/test_pi_protocol.py`
- Create: `tests/runtime/test_builtin_react.py`
- Create: `bridges/pi/test/protocol.test.ts`

**Interfaces:**
- Consumes AgentTask, ModelAdapter, ToolRegistry, Skill manifest, Sandbox, limits, and EventSink.
- Produces `AgentRuntime.run`, `BuiltinReActRuntime.run`, `PiAgentRuntime.run`, and the `motte.pi.bridge` JSONL protocol with init, ready, run, tool_result, interrupt, model_call, tool_call, state, artifact, final, and error messages.

- [ ] **Step 1: Write failing protocol tests**

```python
def test_bridge_event_has_protocol_version_sequence_and_run_id():
    message = BridgeMessage.model_validate({
        "protocol": "motte.pi.bridge",
        "version": 1,
        "run_id": "run-1",
        "seq": 1,
        "type": "ready",
        "payload": {},
    })
    assert message.seq == 1
```

- [ ] **Step 2: Run Python and TypeScript tests to verify they fail**

Run: `uv run pytest tests/runtime/test_pi_protocol.py -q` and `pnpm --dir bridges/pi test`  
Expected: FAIL because the bridge protocol is absent.

- [ ] **Step 3: Implement BuiltinReActRuntime**

Implement a provider-agnostic loop that appends model responses and tool results to state, validates tool arguments, dispatches through ToolRegistry, enforces max steps/tokens/time/cost, emits model/tool/state/termination events, and returns a typed AgentResult. Add tests for no-tool completion, one tool roundtrip, malformed arguments, budget termination, denied tools, and provider errors.

- [ ] **Step 4: Implement the Node bridge**

Pin `@mariozechner/pi-agent-core` and `@mariozechner/pi-ai` versions. Read JSONL from stdin, create a Pi session, inject model/tools/skills, emit typed events to stdout, and never write application data directly.

- [ ] **Step 5: Implement Python PiAgentRuntime**

Spawn the bridge with a controlled environment and working directory, validate every incoming message, forward tool results, enforce budgets and cancellation, and convert bridge messages to TraceEvent.

- [ ] **Step 6: Add integration fixture**

Use a fake Pi model and mock tools to verify tool selection, tool result roundtrip, final output, interrupt, bridge crash, and sequence ordering without paid API calls.

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/runtime/test_pi_protocol.py -q`; `pnpm --dir bridges/pi test`; `uv run pytest tests/integration -k pi -q`  
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add packages/agent-runtime bridges/pi tests/runtime tests/integration
git commit -m "feat: add pi agent runtime adapter"
```

## Task 8: Claude and Codex Harness Adapters

**Files:**
- Create: `packages/harness-runtime/motte_harness/protocol.py`
- Create: `packages/harness-runtime/motte_harness/channel.py`
- Create: `packages/harness-runtime/motte_harness/process.py`
- Create: `packages/harness-runtime/motte_harness/claude.py`
- Create: `packages/harness-runtime/motte_harness/codex.py`
- Create: `packages/harness-runtime/motte_harness/probe.py`
- Create: `tests/harness/test_claude_parser.py`
- Create: `tests/harness/test_codex_parser.py`
- Create: `tests/harness/test_process_lifecycle.py`
- Create: `tests/harness/test_channel.py`

**Interfaces:**
- Consumes HarnessRun, SandboxPolicy, and process limits.
- Produces `HarnessAdapter.prepare/start/send/events/interrupt/collect/cleanup`, parser versions, executable probes, and normalized events.
- Produces `HarnessChannel.send/receive`, parser versions, executable probes, and normalized events.

- [ ] **Step 1: Write failing parser tests from recorded JSONL fixtures**

```python
def test_claude_parser_emits_tool_call_and_final():
    events = list(parse_claude_stream(load_fixture("claude_stream.jsonl")))
    assert [event.type for event in events] == ["tool_call", "tool_result", "final"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/harness -q`  
Expected: FAIL because process adapters and parsers are absent.

- [ ] **Step 3: Implement process supervision**

Use asyncio subprocess pipes, bounded stdout/stderr readers, cancellation, timeout, exit-code capture, and controlled environment variables. On Linux/WSL2 terminate POSIX process groups; on Windows use Job Objects or an equivalent process-tree terminator and record the host capability. Store raw lines as artifacts before parsing.

- [ ] **Step 4: Implement bidirectional HarnessChannel**

Implement `send` through a bounded Worker command queue. Local CLI adapters write only to controlled stdin; Codex app-server writes JSON-RPC requests. Reject messages after terminal state, persist `user_message`/`harness_request`/`harness_response`, and expose the same channel to API, CLI, and Web.

- [ ] **Step 5: Implement Claude adapter**

Launch print mode with `--output-format stream-json`, optional partial messages, explicit max turns and budget, parse assistant/tool/result/system events, and collect session metadata and workspace diff.

- [ ] **Step 6: Implement Codex adapters**

Implement app-server stdio JSON-RPC with thread/turn lifecycle and approval notifications. Add exec batch parsing as a separate adapter. Preserve event cursors, token usage, diffs, and failure status.

- [ ] **Step 7: Implement version probes and compatibility checks**

Run executable version commands, verify required flags/protocol behavior with a no-op fixture, and mark unknown versions unsupported before a paid Run.

- [ ] **Step 8: Run tests**

Run: `uv run pytest tests/harness -q`  
Expected: PASS, including malformed lines, process timeout, cancellation, approval, parser version, and artifact collection.

- [ ] **Step 9: Commit**

```bash
git add packages/harness-runtime tests/harness
git commit -m "feat: add claude and codex harness adapters"
```

## Task 9: Evaluators, Aggregation, and Reports

**Files:**
- Create: `packages/evaluators/motte_eval/base.py`
- Create: `packages/evaluators/motte_eval/deterministic.py`
- Create: `packages/evaluators/motte_eval/trajectory.py`
- Create: `packages/evaluators/motte_eval/judge.py`
- Create: `packages/evaluators/motte_eval/aggregate.py`
- Create: `packages/evaluators/motte_eval/gates.py`
- Create: `packages/harness-runtime/motte_harness/inspect.py`
- Create: `tests/evaluators/test_deterministic.py`
- Create: `tests/evaluators/test_trajectory.py`
- Create: `tests/evaluators/test_aggregate.py`

**Interfaces:**
- Consumes Observation, TraceEvent, Artifact, ModelAdapter, and evaluator configuration.
- Produces `Evaluator.evaluate`, metric evidence, aggregate statistics, pairwise comparison, pass@k, and confidence metadata.
- Produces `RegressionGate`, `InspectHarness`, and gate decisions for CI or local runs.

- [ ] **Step 1: Write failing evaluator tests**

```python
def test_json_schema_evaluator_returns_evidence_on_failure():
    score = JsonSchemaEvaluator(schema={"type": "object", "required": ["name"]}).evaluate({"age": 3})
    assert score.passed is False
    assert score.evidence[0].path == "$.name"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/evaluators -q`  
Expected: FAIL because evaluators are absent.

- [ ] **Step 3: Implement deterministic and trajectory evaluators**

Implement JSON Schema, exact match, regex, exit code, unit test, file diff, tool selection/arguments/order, step coverage, budget, goal success, and secret/PII leak checks. Every result references the observation or artifact evidence used.

- [ ] **Step 4: Implement LLM judge and calibration metadata**

Use ModelAdapter with fixed judge model, prompt version, temperature policy, rubric version, and optional repeated sampling. Store judge request/response separately from the subject Run.

- [ ] **Step 5: Implement aggregation and comparisons**

Aggregate per case, Scenario, model, runtime, and repeat. Calculate means, quantiles, pass@k, cost, latency, failure classes, pairwise deltas, and confidence intervals. Calculate cost from the versioned PriceTable and response usage breakdown, attach `price_table_version` to every cost score, and leave cost unknown when no price is confirmed. Preserve missing and unsupported cases rather than treating them as zero.

- [ ] **Step 6: Implement InspectHarness and regression gates**

Map Scenario Dataset/Case to Inspect Task, Agent/solver to Solver, evaluator configuration to Scorer, and Docker spec to Inspect sandbox. Convert Inspect logs into TraceEvent and make its version part of the manifest. Implement gates that compare selected metrics against a pinned baseline with threshold, direction, missing-data, and unsupported-case policies; a failed gate returns a non-zero CLI exit code and a structured report.

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/evaluators -q`  
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add packages/evaluators packages/harness-runtime/motte_harness/inspect.py tests/evaluators
git commit -m "feat: add evidence based evaluators and reports"
```

## Task 10: API, Celery Worker, and CLI

**Files:**
- Create: `apps/api/app/main.py`
- Create: `apps/api/app/dependencies.py`
- Create: `apps/api/app/routers/providers.py`, `models.py`, `datasets.py`, `scenarios.py`, `agents.py`, `skills.py`, `harnesses.py`, `runs.py`, `reports.py`
- Create: `apps/worker/motte_worker/celery_app.py`, `tasks.py`
- Create: `packages/cli/motte_cli/main.py`, `commands/*.py`, `render.py`
- Create: `tests/api/test_runs.py`
- Create: `tests/cli/test_json_output.py`

**Interfaces:**
- Consumes repositories, RunExecutor, Provider catalog, Runtime registry, Evaluators, and HarnessChannel.
- Produces `/api/v1` resource endpoints, SSE Run events, `POST /runs/{id}/messages` for active Harness sessions, Celery task dispatch, and CLI commands from the approved design.

- [ ] **Step 1: Write failing API and CLI tests**

```python
async def test_create_run_returns_queued_run(client):
    response = await client.post("/api/v1/runs", json={"scenario_version": "json_extract@1"})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"

async def test_create_run_rejects_unsupported_capability(client):
    response = await client.post("/api/v1/runs", json={"scenario_version": "vision@1"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_CAPABILITY_UNSUPPORTED"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api tests/cli -q`  
Expected: FAIL because the API and CLI do not exist.

- [ ] **Step 3: Implement API routers and dependency wiring**

Expose Provider test/model sync, model profile refresh/override, dataset/scenario versioning, agent/skill/harness inspection, Run create/status/events/artifacts/scores/cancel/retry/rescore/replay/messages, reports, and health. Perform capability validation synchronously; return 202 with persisted `queued` only after validation, and return 422 without creating a Run on failure. Return contract errors with stable codes.

- [ ] **Step 4: Implement worker tasks**

Dispatch one idempotent task per Run, publish progress to Redis, persist every state transition, and send cancellation to the active runtime. Keep API request handlers free of model or sandbox execution.

- [ ] **Step 5: Implement CLI command groups**

Implement `provider`, `model`, `dataset`, `scenario`, `agent`, `skill`, `harness`, `run`, `report`, and `doctor`. Support table, JSON, and JSONL output and a `--server` endpoint without duplicating validation logic.

- [ ] **Step 6: Run tests and contract generation**

Run: `uv run pytest tests/api tests/cli -q`; `uv run python -m motte_contracts.jsonschema`; `uv run python -m motte_cli --help`  
Expected: PASS, generated schemas, and complete command help.

- [ ] **Step 7: Commit**

```bash
git add apps/api apps/worker packages/cli tests/api tests/cli
git commit -m "feat: add api worker and cli control plane"
```

## Task 11: Web Console

**Files:**
- Create: `apps/web/src/api/client.ts`
- Create: `apps/web/src/routes/*.tsx`
- Create: `apps/web/src/components/RunTimeline.tsx`
- Create: `apps/web/src/components/ModelProfileEditor.tsx`
- Create: `apps/web/src/components/ScenarioEditor.tsx`
- Create: `apps/web/src/components/ScoreTable.tsx`
- Create: `apps/web/src/types/generated.ts`
- Create: `apps/web/tests/*.test.tsx`

**Interfaces:**
- Consumes generated TypeScript schemas and `/api/v1` endpoints from Task 10.
- Produces pages for Providers, Models, Datasets, Scenarios, Agents, Skills, Harnesses, Runs, Trace, Compare, and Reports.

- [ ] **Step 1: Write failing UI tests**

```tsx
it("renders model capability and token limits", async () => {
  render(<ModelProfileEditor profile={fixtureProfile} />);
  expect(screen.getByText("工具调用")).toBeVisible();
  expect(screen.getByText("上下文窗口")).toBeVisible();
  expect(screen.getByText("最大输出 token")).toBeVisible();
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pnpm --dir apps/web test`  
Expected: FAIL because the components are absent.

- [ ] **Step 3: Generate client types from OpenAPI**

Generate `apps/web/src/types/generated.ts` in CI and reject uncommitted generated changes. Keep API models separate from view models.

- [ ] **Step 4: Implement resource pages and validation feedback**

Add model capability/limit/reasoning editors, Scenario schema and capability validation, run controls, live SSE timeline, artifact links, trace filtering, comparison, and report export. Never render credential values.

- [ ] **Step 5: Run UI and API integration tests**

Run: `pnpm --dir apps/web test`; `uv run pytest tests/integration -k web -q`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/web
git commit -m "feat: add evaluation web console"
```

## Task 12: Integrated Verification, Packaging, and Release Process

**Files:**
- Create: `tests/integration/test_direct_llm_run.py`
- Create: `tests/integration/test_pi_skill_sandbox_run.py`
- Create: `tests/integration/test_claude_codex_harness_run.py`
- Create: `tests/fixtures/replay/*.jsonl`
- Modify: `.github/workflows/ci.yml`
- Create: `scripts/check_contracts.ps1`
- Create: `scripts/doctor.ps1`
- Modify: `README.md`
- Create: `docs/operations/install.md`
- Create: `docs/operations/upgrade.md`
- Create: `docs/protocols/provider-compatibility.md`
- Create: `docs/README.md`

**Interfaces:**
- Consumes all packages and services from Tasks 1–11.
- Produces a repeatable local install, replay-only CI suite, API/Schema compatibility checks, release artifacts, and operational documentation.

- [ ] **Step 1: Add replay-only end-to-end fixtures**

```python
async def test_direct_llm_replay_run_is_completed(app):
    run = await run_scenario(app, "scenarios/json_extract_replay.yaml")
    assert run.status == "completed"
    assert run.scores[0].passed is True
```

- [ ] **Step 2: Run the replay suite to verify the integrated path**

Run: `uv run pytest tests/integration -m replay -q`  
Expected: FAIL until all preceding packages are wired together.

- [ ] **Step 3: Implement integration fixtures and doctor checks**

Provide direct LLM, Pi + Skill + Docker, Claude, and Codex scenarios that use recorded model/CLI output for CI. `motte doctor` verifies Python/Node/Docker/PostgreSQL/Redis, bridge versions, executable probes, artifact permissions, host capability matrix, and schema compatibility. Link the long-lived documentation entry from `docs/README.md` to the spec, plan, and ADR.

- [ ] **Step 4: Expand CI gates**

Run contract tests, type checks, lint, migration checks, replay integration tests, Web tests, OpenAPI diff checks, and dependency/image scanning. Keep paid Provider calls outside default CI and provide an explicit operator command for live smoke tests.

- [ ] **Step 5: Package and document install/upgrade**

Build Python wheels, CLI package, Web assets, Pi bridge bundle, and Docker images. Document backup, migration, profile refresh, adapter compatibility, artifact cleanup, and rollback to a matching release.

- [ ] **Step 6: Run the complete verification command set**

Run: `uv run ruff check .`; `uv run mypy packages apps`; `uv run pytest -q`; `pnpm -r lint`; `pnpm -r test`; `docker compose -f infra/docker-compose.yml config`  
Expected: PASS with no contract, type, lint, test, or Compose errors.

- [ ] **Step 7: Commit**

```bash
git add tests/integration tests/fixtures .github scripts README.md docs docs/operations docs/protocols
git commit -m "chore: add integrated verification and release docs"
```

## Cross-Task Acceptance Gates

1. A Scenario requiring a missing tool, modality, context window, output limit, or reasoning level fails before paid execution with a structured `unsupported` result.
2. A replay Run produces the same TraceEvent ordering, artifacts, and deterministic scores as the recorded fixture.
3. A direct LLM Run, Pi Run, Claude Harness Run, and Codex Harness Run all produce the same public Run/Trace/Artifact/Score contract.
4. Rescoring never calls the subject model or external CLI again.
5. Cancelling a Run terminates the active worker, bridge, CLI process, and Docker container, then records the termination reason.
6. Secrets are absent from API responses, rendered Web state, raw trace payloads, and artifact listings.
7. A version change in a Provider, Pi bridge, Claude CLI, or Codex CLI is detected by probe and either passes its compatibility range or becomes `unsupported`.
8. The local installation, migration, backup, restore, and rollback procedures are executable from the operations documentation.

## Plan Self-Review

- Spec coverage: architecture, provider profiles and PriceTable, Dataset JSONL, LiteLLM transport boundary, BuiltinReAct, Pi, Skill, Claude/Codex Harness, Inspect, Scenario lifecycle, sandbox policy layers, evidence retention, scheduling, API, CLI, Web, storage, versioning, and staged delivery are covered by Tasks 1–12.
- Completeness scan: no unspecified implementation step remains; later capabilities are represented as named tasks with concrete interfaces and tests.
- Type consistency: all later tasks consume contracts from Task 2; storage, runtimes, evaluators, API, CLI, and Web use the same Run/Trace/Artifact/Score objects; Pi and CLI adapters communicate through explicit protocol contracts.
- Scope: the plan is single-user and local, while storage and adapter interfaces remain replaceable without adding multi-user behavior.
