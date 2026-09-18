from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from motte_contracts.compat import adapt_legacy_report, adapt_legacy_run, adapt_legacy_trace_event
from motte_contracts.events import TraceEvent
from motte_contracts.report import ReportSummary, RunReport
from motte_contracts.run import Run, RunCommand
from motte_sdk.execution_backends import legacy_execution
from motte_sdk.resolve import ManifestResolutionError, find_secret_paths, prepare_run, resolve_manifest
from motte_sdk.service import RunService, build_run_service
from motte_storage import RunConflictError
from motte_storage.factory import create_resource_store
from motte_storage.resource_store import InMemoryResourceStore, ResourceConflictError

from apps.api.app.schemas import (
    CancelRunRequest,
    CreateRunRequest,
    ReplayRunRequest,
    RunCommandListResponse,
    RunListResponse,
    RunMessageRequest,
    ScoringPassListResponse,
)

SSE_POLL_INTERVAL_SECONDS = 1.0

AGENT_CATALOG = [
    {
        "id": "builtin-react", "kind": "react",
        "description": "内置 ReAct agent（尚未接入 ExecutionBackend）",
        "protocol_ready": True, "execution_ready": False,
    },
    {
        "id": "pi", "kind": "bridge", "description": "Pi Agent bridge protocol v1",
        "protocol_ready": True, "execution_ready": False,
    },
]

HARNESS_CATALOG = ["claude", "codex"]
BUILTIN_SCENARIOS = {"replay@1", "json_extract@1", "direct-llm@1", "vision@1"}

# kind 展示目录：协议能力来自 motte_provider 注册表，这里只补 UI 文案与官方默认端点
# （base_url 与 request_path 由 transport 直接拼接，官方端点须含版本段）。
PROVIDER_KIND_CATALOG = {
    "openai_compatible": {
        "label": "OpenAI 兼容",
        "description": "任意兼容 Chat Completions 的端点（vLLM、Ollama、网关等）",
        "default_base_url": None,
    },
    "anthropic_messages": {
        "label": "Anthropic Messages",
        "description": "Anthropic Messages API（官方或兼容端点）",
        "default_base_url": "https://api.anthropic.com/v1",
    },
    "openai_responses": {
        "label": "OpenAI Responses",
        "description": "OpenAI Responses API（官方或兼容端点）",
        "default_base_url": "https://api.openai.com/v1",
    },
}

_FORBIDDEN_SECRET_FIELDS = ("api_key", "key", "token", "secret", "password", "authorization")


def _validate_provider(provider_config: dict):
    """strict 预检：不合法的 provider 配置在创建阶段就拒绝，不产生任何付费调用。"""
    from motte_provider.capabilities import UnsupportedParameterError
    from motte_provider.config import validate_provider_config

    try:
        validate_provider_config(provider_config)
    except UnsupportedParameterError as error:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "UNSUPPORTED_PARAMETER", "message": str(error)}},
        )
    except ValueError as error:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "PROVIDER_CONFIG_INVALID", "message": str(error)}},
        )
    return None


def _reject_secret_fields(body: dict):
    leaked = find_secret_paths(body)
    if leaked:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "CREDENTIALS_REJECTED",
                    "message": f"凭据字段不接受明文存储：{leaked}；密钥请配置在凭据文件（python -m motte_cli credentials set）或 api_key_env 变量名",
                }
            },
        )
    return None


def _suite_mismatch(resource: dict[str, Any], expected: str, reference: str) -> JSONResponse | None:
    from motte_contracts import suites as contract_suites

    actual = contract_suites.suite_of(resource)
    if actual == expected:
        return None
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "SUITE_MISMATCH",
            "message": f"resource {reference} belongs to suite {actual!r}, expected {expected!r}",
        }},
    )


def _resource_hash(record: dict[str, Any]) -> str:
    ignored = {"profile_hash", "generation", "lifecycle", "published_at", "deprecated_at"}
    payload = {key: value for key, value in record.items() if key not in ignored}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_app(store=None, resource_store=None) -> FastAPI:
    service = build_run_service() if store is None else RunService(store)
    resources = resource_store if resource_store is not None else (
        create_resource_store() if store is None else InMemoryResourceStore()
    )
    application = FastAPI(title="MoTTEavl API", version="0.1.0")
    application.state.run_service = service
    application.state.resource_store = resources

    @application.exception_handler(ResourceConflictError)
    async def resource_conflict(request, error):
        return JSONResponse(status_code=409, content={"error": {"code": "RESOURCE_CONFLICT", "message": str(error)}})

    @application.exception_handler(RunConflictError)
    async def run_conflict(request, error):
        return JSONResponse(status_code=409, content={"error": {
            "code": "RUN_CONFLICT", "message": "run revision or status changed",
        }})

    # ------------------------------------------------------------------ runs

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post(
        "/api/v1/runs", status_code=202, response_model=Run,
        response_model_exclude_unset=True,
    )
    def create_run(request_body: CreateRunRequest):
        body = request_body.model_dump()
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        scenario = body.get("scenario_version", "")
        if not isinstance(scenario, str) or not scenario:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "SCENARIO_REQUIRED", "message": "scenario_version is required"}},
            )
        if scenario.startswith("vision@"):
            return JSONResponse(status_code=422, content={"error": {"code": "MODEL_CAPABILITY_UNSUPPORTED", "message": "vision capability is unsupported"}})
        if scenario not in BUILTIN_SCENARIOS:
            try:
                scenario_name, scenario_version = scenario.rsplit("@", 1)
            except ValueError:
                scenario_name, scenario_version = scenario, ""
            if not scenario_name or resources.scenarios.get(scenario_name, scenario_version) is None:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "SCENARIO_NOT_FOUND",
                            "message": f"scenario not found: {scenario}",
                        }
                    },
                )
        requested_manifest = body.get("manifest") or {}
        manifest = requested_manifest
        provider_config = manifest.get("provider")
        if scenario.startswith("direct-llm@") and not provider_config and not manifest.get("model"):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "PROVIDER_REQUIRED",
                        "message": "direct-llm runs require manifest.provider or manifest.model",
                    }
                },
            )
        try:
            manifest, case_ids = prepare_run(scenario, manifest, body.get("case_ids", []), resources)
        except ManifestResolutionError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": error.code, "message": str(error)}},
            )
        execution = manifest.get("execution") or {}
        provider = manifest.get("provider") or {}
        fixture = provider.get("fixture") if isinstance(provider, dict) else None
        if execution.get("backend_id") == "replay" and fixture is not None:
            if not isinstance(fixture, dict) or not fixture or any(
                not isinstance(case_id, str) or not case_id
                or not isinstance(item, dict) or "output" not in item
                for case_id, item in fixture.items()
            ):
                return JSONResponse(status_code=422, content={"error": {
                    "code": "REPLAY_FIXTURE_INVALID",
                    "message": "replay fixture must contain object cases with output fields",
                }})
            if not case_ids:
                case_ids = list(fixture)
            missing = [case_id for case_id in case_ids if case_id not in fixture]
            if missing:
                return JSONResponse(status_code=422, content={"error": {
                    "code": "REPLAY_CASE_NOT_FOUND",
                    "message": f"replay fixture has no selected cases: {missing}",
                }})
        if isinstance(manifest.get("provider"), dict):
            invalid = _validate_provider(manifest["provider"])
            if invalid is not None:
                return invalid
        return service.create_run(
            scenario, manifest, case_ids, requested_manifest=requested_manifest
        )

    @application.get(
        "/api/v1/runs", response_model=RunListResponse,
        response_model_exclude_unset=True,
    )
    def list_runs(status: str | None = None):
        runs = service.store.runs.list()
        if status is not None:
            runs = [run for run in runs if run.get("status") == status]
        items = [
            adapt_legacy_run({**run, "model": _run_model_label(run)}) for run in runs
        ]
        return {"items": items, "total": len(items)}

    @application.get(
        "/api/v1/runs/{run_id}", response_model=Run,
        response_model_exclude_unset=True,
    )
    def get_run(run_id: str):
        try:
            return adapt_legacy_run(service.get_run(run_id))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error

    @application.post(
        "/api/v1/runs/{run_id}/cancel", response_model=Run,
        response_model_exclude_unset=True,
    )
    def cancel_run(run_id: str, body: CancelRunRequest | None = None):
        try:
            return service.cancel(run_id, reason=body.reason if body is not None else None)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/runs/{run_id}/rescore", response_model=Run,
        response_model_exclude_unset=True,
    )
    def rescore_run(run_id: str):
        try:
            return service.rescore(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/runs/{run_id}/retry", response_model=Run,
        response_model_exclude_unset=True,
    )
    def retry_run(run_id: str):
        try:
            parent = service.get_run(run_id)
            refreshed = None
            refreshed_case_ids = None
            if parent["status"] == "profile_stale":
                requested = parent.get("requested_manifest")
                if not isinstance(requested, dict):
                    raise ValueError(
                        "profile_stale retry cannot refresh a legacy run without requested_manifest"
                    )
                requested = deepcopy(requested)
                managed = (parent.get("manifest") or {}).get("benchmark_provenance")
                if managed:
                    pinned_selection = (parent.get("manifest") or {}).get("case_selection")
                    requested_selection = requested.get("case_selection")
                    has_explicit_ids = (
                        isinstance(requested_selection, dict)
                        and requested_selection.get("mode") == "ids"
                        and isinstance(requested_selection.get("case_ids"), list)
                        and bool(requested_selection["case_ids"])
                    )
                    if isinstance(pinned_selection, dict) and not has_explicit_ids:
                        requested["case_selection"] = deepcopy(pinned_selection)
                    selected = []
                else:
                    selected = parent.get("case_ids") or []
                refreshed, refreshed_case_ids = prepare_run(
                    parent["scenario_version"], requested, selected, resources
                )
            return service.retry(
                run_id, refreshed_manifest=refreshed, case_ids=refreshed_case_ids
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        except (ManifestResolutionError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/runs/{run_id}/messages",
        status_code=409,
        response_model=None,
        responses={
            404: {"description": "Run not found"},
            409: {"description": "Command unsupported for this run"},
            501: {"description": "No command consumer is registered"},
        },
    )
    def send_run_message(run_id: str, body: RunMessageRequest):
        try:
            run = service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        if run["status"] in RunService.TERMINAL:
            raise HTTPException(status_code=409, detail="run is not active")
        execution = (run.get("manifest") or {}).get("execution") or {}
        capabilities = execution.get("capabilities") or {}
        if capabilities.get("interactive") is not True:
            return JSONResponse(
                status_code=409,
                content={"error": {
                    "code": "COMMAND_UNSUPPORTED",
                    "message": "the run execution backend is not interactive",
                }},
            )
        return JSONResponse(
            status_code=501,
            content={"error": {
                "code": "RUN_COMMANDS_NOT_IMPLEMENTED",
                "message": "no registered execution backend has a durable command consumer",
            }},
        )

    @application.get(
        "/api/v1/runs/{run_id}/commands", response_model=RunCommandListResponse
    )
    def list_run_commands(run_id: str):
        try:
            service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        items = service.store.commands.list_for_run(run_id)
        return {"items": items, "total": len(items)}

    @application.post(
        "/api/v1/runs/{run_id}/replay", status_code=202, response_model=Run,
        response_model_exclude_unset=True,
    )
    def replay_run(run_id: str, body: ReplayRunRequest):
        fixture = {
            case_id: item.model_dump(mode="json", exclude_unset=True)
            for case_id, item in body.cases.items()
        }
        rejected = _reject_secret_fields({"cases": fixture})
        if rejected is not None:
            return rejected
        try:
            run = service._load(run_id)
            if run["status"] != "queued":
                raise ValueError("only queued runs can receive a replay fixture")
            manifest = deepcopy(run.get("manifest") or {})
            if not isinstance(manifest.get("execution"), dict):
                manifest = legacy_execution({**run, "manifest": manifest})
            execution = manifest.get("execution") or {}
            if execution.get("backend_id") != "replay":
                raise ValueError("run is not configured for replay execution")
            if manifest.get("benchmark_provenance"):
                raise ValueError("managed benchmark runs cannot receive replay fixtures")
            existing_provider = manifest.get("provider")
            provider_fixture = (
                existing_provider.get("fixture")
                if isinstance(existing_provider, dict) and "fixture" in existing_provider
                else None
            )
            has_provider_fixture = isinstance(existing_provider, dict) and "fixture" in existing_provider
            has_manifest_fixture = "replay_fixture" in manifest
            manifest_fixture = manifest.get("replay_fixture") if has_manifest_fixture else None
            if has_provider_fixture and has_manifest_fixture and provider_fixture != manifest_fixture:
                raise ValueError("provider.fixture and replay_fixture are inconsistent")
            existing_fixture = provider_fixture if has_provider_fixture else manifest_fixture
            if has_provider_fixture or has_manifest_fixture:
                if existing_fixture != fixture:
                    raise ValueError("replay fixture is immutable once configured")
            selected = list(run.get("case_ids") or fixture)
            missing = [case_id for case_id in selected if case_id not in fixture]
            if missing:
                raise ValueError(f"replay fixture has no selected cases: {missing}")
            manifest.pop("replay_fixture", None)
            manifest["provider"] = {
                **(existing_provider if isinstance(existing_provider, dict) else {}),
                "kind": "replay",
                "fixture": fixture,
            }
            requested = deepcopy(run.get("requested_manifest") or {})
            # Keep the user's provider/model reference resolvable for profile-stale retry;
            # the fixture is run data, not a replacement for that reference.
            requested["replay_fixture"] = fixture
            run.update({
                "manifest": manifest,
                "requested_manifest": requested,
                "case_ids": selected,
                "updated_at": datetime.now(UTC).isoformat(),
            })
            service.store.runs.update(
                run,
                expected_revision=run["revision"],
                expected_status="queued",
            )
            return service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        except (RunConflictError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/v1/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        try:
            service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        last_event_id = request.headers.get("last-event-id")
        cursor = max(after, int(last_event_id)) if last_event_id is not None else after

        async def stream():
            nonlocal cursor
            while True:
                for event in service.events_after(run_id, cursor):
                    cursor = event["seq"]
                    envelope = TraceEvent.model_validate(
                        adapt_legacy_trace_event(event)
                    ).model_dump(mode="json", exclude_none=True)
                    yield f"id: {event['seq']}\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"
                if service.get_run(run_id)["status"] in RunService.TERMINAL:
                    return
                yield ": ping\n\n"
                await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @application.get(
        "/api/v1/runs/{run_id}/report", response_model=RunReport,
        response_model_exclude_unset=True,
    )
    def run_report(run_id: str, scoring_pass_id: str | None = None):
        try:
            run = service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        if scoring_pass_id is not None:
            selected = service.store.scoring_passes.get(scoring_pass_id)
            if selected is None or selected.get("run_id") != run_id:
                return JSONResponse(status_code=404, content={"error": {
                    "code": "SCORING_PASS_NOT_FOUND",
                    "message": f"scoring pass not found for run: {scoring_pass_id}",
                }})
            run["scores"] = service.store.score_sets.list_for_pass(scoring_pass_id)
            run["current_scoring_pass_id"] = scoring_pass_id
            run["scoring_pass"] = selected
        return adapt_legacy_report(_build_report(adapt_legacy_run(run)))

    @application.get(
        "/api/v1/runs/{run_id}/scoring-passes", response_model=ScoringPassListResponse
    )
    def list_scoring_passes(run_id: str):
        try:
            service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        items = service.store.scoring_passes.list_for_run(run_id)
        return {"items": items, "total": len(items)}

    # ------------------------------------------------------- 资源 CRUD

    def _resource_routes(name: str, validate, *, versioned: bool, key_is_path: bool = False):
        repository = getattr(resources, name)

        @application.get(f"/api/v1/{name}")
        def list_items():
            items = repository.list()
            return {"items": items, "total": len(items)}

        @application.post(f"/api/v1/{name}", status_code=201)
        def create_item(body: dict):
            server_managed = {"_deleted"} | {
                "providers": {"generation"},
                "models": {
                    "generation", "lifecycle", "profile_hash", "published_at", "deprecated_at"
                },
            }.get(name, set())
            supplied_managed = sorted(set(body).intersection(server_managed))
            if supplied_managed:
                return JSONResponse(status_code=422, content={"error": {
                    "code": "SERVER_MANAGED_FIELD",
                    "message": f"server-managed fields are not accepted: {supplied_managed}",
                }})
            rejected = _reject_secret_fields(body)
            if rejected is not None:
                return rejected
            invalid = validate(body)
            if invalid is not None:
                return invalid
            if not versioned:
                resource_key = body.get("name") if name == "providers" else body.get("id")
                if resource_key and repository.get(resource_key) is not None:
                    return JSONResponse(status_code=409, content={"error": {
                        "code": "RESOURCE_CONFLICT",
                        "message": f"{name[:-1]} already exists: {resource_key}",
                    }})
            try:
                return repository.put(body, expected_generation=0 if not versioned else None)
            except ResourceConflictError:
                raise
            except ValueError as error:
                return JSONResponse(status_code=422, content={"error": {"code": "CONTRACT_INVALID", "message": str(error)}})

        if versioned:
            path = f"/api/v1/{name}/{{resource_name}}/{{version}}"

            @application.get(path)
            def get_versioned(resource_name: str, version: str):
                record = repository.get(resource_name, version)
                if record is None:
                    raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
                return record

            @application.delete(path)
            def delete_versioned(resource_name: str, version: str):
                if repository.get(resource_name, version) is None:
                    raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
                return JSONResponse(status_code=409, content={"error": {
                    "code": "PUBLISHED_RESOURCE_IMMUTABLE",
                    "message": f"published resource cannot be deleted: {resource_name}@{version}",
                }})

        else:
            # 资源键可能自带路径分隔符（如模型 id `Qwen/Qwen2.5-7B`）：整键交给路由，不做路径切分
            path = f"/api/v1/{name}/{{key:path}}" if key_is_path else f"/api/v1/{name}/{{key}}"

            @application.get(path)
            def get_item(key: str):
                record = repository.get(key)
                if record is None:
                    raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
                return record

            @application.delete(path)
            def delete_item(key: str):
                record = repository.get(key)
                if record is None:
                    raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
                if name == "models":
                    if record.get("lifecycle") == "deprecated":
                        return {"deprecated": key}
                    deprecated = {
                        **record,
                        "lifecycle": "deprecated",
                        "deprecated_at": datetime.now(UTC).isoformat(),
                        "generation": int(record.get("generation", 1)) + 1,
                    }
                    invalid = validate(deprecated)
                    if invalid is not None:
                        return invalid
                    repository.put(
                        deprecated, expected_generation=int(record.get("generation", 1))
                    )
                    return {"deprecated": key}
                if not repository.delete(
                    key, expected_generation=int(record.get("generation", 1))
                ):
                    raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
                return {"deleted": key}

    def _validate_provider_resource(body: dict):
        from motte_provider.config import validate_connection_config

        if not body.get("name"):
            return JSONResponse(status_code=422, content={"error": {"code": "PROVIDER_CONFIG_INVALID", "message": "name is required"}})
        body.setdefault("generation", 1)
        try:
            validate_connection_config(body)
        except ValueError as error:
            return JSONResponse(status_code=422, content={"error": {"code": "PROVIDER_CONFIG_INVALID", "message": str(error)}})
        return None

    def _validate_model(body: dict):
        from pydantic import ValidationError

        try:
            from motte_contracts.model import ModelProfile

            profile = ModelProfile.model_validate(body)
            normalized = profile.model_dump(mode="json")
            normalized["parameters"] = profile.parameters.model_dump(mode="json", exclude_none=True)
            normalized["profile_hash"] = _resource_hash(normalized)
            body.clear()
            body.update(normalized)
        except ValidationError as error:
            issue = error.errors()[-1]
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID", "message": issue["msg"], "field": str(issue["loc"])}},
            )
        provider_name = body.get("provider")
        if provider_name and resources.providers.get(provider_name) is None:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "RESOURCE_NOT_FOUND", "message": f"provider not found: {provider_name}"}},
            )
        return None

    def _validate_named_version(body: dict):
        for field in ("name", "version"):
            if not body.get(field):
                return JSONResponse(status_code=422, content={"error": {"code": "CONTRACT_INVALID", "message": f"{field} is required"}})
        return None

    def _validate_price_table(body: dict):
        from motte_provider.pricing import parse_price_table

        for field in ("model_id", "version"):
            if not body.get(field):
                return JSONResponse(status_code=422, content={"error": {"code": "CONTRACT_INVALID", "message": f"{field} is required"}})
        try:
            parse_price_table(body)
        except ValueError as error:
            return JSONResponse(status_code=422, content={"error": {"code": "CONTRACT_INVALID", "message": str(error)}})
        return None

    _resource_routes("providers", _validate_provider_resource, versioned=False, key_is_path=True)
    _resource_routes("models", _validate_model, versioned=False, key_is_path=True)
    _resource_routes("datasets", _validate_named_version, versioned=True)
    _resource_routes("scenarios", _validate_named_version, versioned=True)
    _resource_routes("price_tables", _validate_price_table, versioned=True)

    # 读-改-写更新：只接受白名单字段，其余载荷原样保留（put 是整记录覆盖，先合再校验）
    PROVIDER_UPDATABLE_FIELDS = (
        "kind", "base_url", "credentials", "api_key_env", "enabled", "request_path",
        "timeout", "max_retries", "backoff_initial_ms", "backoff_max_ms",
    )
    MODEL_UPDATABLE_FIELDS = (
        "model", "enabled", "capabilities", "input_modalities", "output_modalities",
        "supports_tools", "tool_features", "context_window", "max_output_tokens",
        "reasoning", "parameters", "provenance", "identity_policy",
        "identity_aliases", "identity_alias_version",
    )

    @application.put("/api/v1/providers/{name:path}")
    def update_provider(name: str, body: dict):
        record = resources.providers.get(name)
        if record is None:
            raise HTTPException(status_code=404, detail="provider not found")
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        if "name" in body and body["name"] != name:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID", "message": "provider name is immutable"}},
            )
        unknown = sorted(set(body) - set(PROVIDER_UPDATABLE_FIELDS) - {"name"})
        if unknown:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID", "message": f"unknown fields: {unknown}"}},
            )
        merged = {
            **record,
            **{key: body[key] for key in PROVIDER_UPDATABLE_FIELDS if key in body},
        }
        if not isinstance(merged.get("enabled"), bool):
            merged["enabled"] = True
        merged["generation"] = int(record.get("generation", 1)) + 1
        invalid = _validate_provider_resource(merged)
        if invalid is not None:
            return invalid
        return resources.providers.put(
            merged, expected_generation=int(record.get("generation", 1))
        )

    @application.put("/api/v1/models/{model_id:path}")
    def update_model(model_id: str, body: dict):
        record = resources.models.get(model_id)
        if record is None:
            raise HTTPException(status_code=404, detail="model not found")
        if record.get("lifecycle", "draft") != "draft":
            return JSONResponse(status_code=409, content={"error": {
                "code": "PUBLISHED_RESOURCE_IMMUTABLE",
                "message": "published or deprecated model profiles cannot be updated",
            }})
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        if "id" in body and body["id"] != model_id:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID", "message": "model id is immutable"}},
            )
        if "provider" in body and body["provider"] != record.get("provider"):
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID", "message": "model provider is immutable"}},
            )
        unknown = sorted(set(body) - set(MODEL_UPDATABLE_FIELDS) - {"id", "provider"})
        if unknown:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID", "message": f"unknown fields: {unknown}"}},
            )
        merged = {
            **record,
            **{key: body[key] for key in MODEL_UPDATABLE_FIELDS if key in body},
            "generation": int(record.get("generation", 1)) + 1,
        }
        invalid = _validate_model(merged)
        if invalid is not None:
            return invalid
        return resources.models.put(
            merged, expected_generation=int(record.get("generation", 1))
        )

    @application.post("/api/v1/models/{model_id:path}/publish")
    def publish_model(model_id: str):
        record = resources.models.get(model_id)
        if record is None:
            raise HTTPException(status_code=404, detail="model not found")
        lifecycle = record.get("lifecycle", "draft")
        if lifecycle == "published":
            return record
        if lifecycle != "draft":
            return JSONResponse(status_code=409, content={"error": {
                "code": "RESOURCE_LIFECYCLE_INVALID",
                "message": f"model cannot be published from lifecycle {lifecycle!r}",
            }})
        published = {
            **record,
            "lifecycle": "published",
            "published_at": datetime.now(UTC).isoformat(),
            "generation": int(record.get("generation", 1)) + 1,
        }
        invalid = _validate_model(published)
        if invalid is not None:
            return invalid
        return resources.models.put(
            published, expected_generation=int(record.get("generation", 1))
        )

    @application.get("/api/v1/provider_kinds")
    def list_provider_kinds():
        """用户可创建的 provider kind 目录（注册表派生；replay 等内部 kind 不暴露）。"""
        from motte_provider.config import registered_kinds
        from motte_provider.registry import adapter_for

        items = []
        for kind in registered_kinds():
            spec = adapter_for(kind)
            if spec.provider_cls is None:
                continue
            catalog = PROVIDER_KIND_CATALOG.get(kind, {})
            items.append(
                {
                    "kind": kind,
                    "label": catalog.get("label", kind),
                    "description": catalog.get("description", ""),
                    "default_base_url": catalog.get("default_base_url"),
                    "default_key_env": spec.default_key_env,
                }
            )
        return {"items": items, "total": len(items)}

    @application.post("/api/v1/models/{model_id:path}/test")
    def test_model(model_id: str, body: dict | None = None):
        """对单个模型做一次最小真实调用（Web 版 live smoke，操作者显式触发）。

        连接、密钥、模型名一次验证：密钥按凭据链解析、绝不进入响应；
        max_output_tokens=16 封顶费用；报告复用 adapter envelope（已脱敏）。
        """
        from motte_contracts.messages import Message, ModelRequest
        from motte_provider.base import ProviderCallError
        from motte_provider.config import adapter_for
        from motte_provider.credentials import resolve_api_key
        from motte_provider.transport import HTTPTransport

        profile = resources.models.get(model_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="model not found")
        connection = resources.providers.get(profile.get("provider") or "")
        if connection is None:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "RESOURCE_NOT_FOUND", "message": f"provider not found: {profile.get('provider')}"}},
            )
        try:
            spec = adapter_for(connection.get("kind"))
        except ValueError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "PROVIDER_CONFIG_INVALID", "message": str(error)}},
            )
        if spec.provider_cls is None or not spec.smoke_supported:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "PROVIDER_TEST_UNSUPPORTED", "message": f"kind 不支持测试调用: {connection.get('kind')}"}},
            )
        from motte_provider.config import build_case_provider

        try:
            manifest = {"model": model_id}
            if "reasoning_level" in (body or {}):
                manifest["reasoning_level"] = body["reasoning_level"]
            config = resolve_manifest(
                manifest, resources, allow_draft_model=True
            )["provider"]
            # Use the same snapshot/factory as real runs, but never exceed 16 output tokens.
            bound = min(16, config.get("max_output_tokens") or 16)
            config["parameters"] = {**(config.get("parameters") or {}), "max_output_tokens": bound}
            config["max_output_tokens"] = bound
            provider = build_case_provider(config).provider
            model = provider.model
            request = ModelRequest(
                model=model,
                messages=[Message(role="user", content=(body or {}).get("prompt") or "Reply with exactly: pong")],
                max_output_tokens=bound,
            )
            envelope = provider.complete(request)
        except ProviderCallError as error:
            envelope = error.evidence
        except ValueError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "MODEL_CONFIG_INVALID", "message": str(error)}},
            )
        metering = envelope.get("metering") or {}
        return {
            "ok": envelope.get("error") is None,
            "provider": connection.get("kind"),
            "model": model,
            "base_url": connection.get("base_url"),
            "tested_at": datetime.now(UTC).isoformat(),
            "latency_ms": metering.get("latency_ms"),
            "attempts": metering.get("attempts"),
            "retry_count": metering.get("retry_count"),
            "usage": envelope.get("usage"),
            "error": envelope.get("error"),
        }

    # ------------------------------------------------------------ 凭据

    @application.get("/api/v1/credentials")
    def list_credentials():
        from motte_provider.credentials import load_credentials, mask

        items = [
            {
                "profile": name,
                "key_hint": mask(section["api_key"])
                if isinstance(section.get("api_key"), str) and section["api_key"]
                else "未配置",
            }
            for name, section in sorted(load_credentials().items())
        ]
        return {"items": items, "total": len(items)}

    @application.put("/api/v1/credentials/{profile}")
    def set_credential(profile: str, body: dict):
        """Web 端写入凭据文件的唯一通道：密钥写后即弃（不落库），响应只回掩码。

        与 CLI `credentials set` 等价，故不走 _reject_secret_fields（那是防明文入库的）。
        """
        from motte_provider.credentials import mask, save_api_key

        api_key = body.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID", "message": "api_key is required"}},
            )
        try:
            save_api_key(profile, api_key.strip())
        except ValueError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID", "message": str(error)}},
            )
        return {"profile": profile, "key_hint": mask(api_key.strip())}

    # ------------------------------------------------------------ 目录

    @application.get("/api/v1/agents")
    def list_agents():
        return {"items": AGENT_CATALOG, "total": len(AGENT_CATALOG)}

    @application.get("/api/v1/harnesses")
    async def list_harnesses():
        from motte_harness.claude import ClaudeHarness
        from motte_harness.codex import CodexHarness

        harnesses = [ClaudeHarness(), CodexHarness()]
        reports = await asyncio.gather(*(harness.inspect() for harness in harnesses))
        items = [
            {**report, "protocol_ready": True, "execution_ready": False}
            for report in reports
        ]
        return {"items": items, "total": len(items)}

    @application.get("/api/v1/skills")
    def list_skills():
        registry = getattr(application.state, "skill_registry", None) or {}
        items = list(registry.values())
        return {"items": items, "total": len(items)}

    @application.post("/api/v1/skills", status_code=201)
    def register_skill(body: dict):
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        missing = [field for field in ("name", "version", "entrypoint") if not body.get(field)]
        if missing:
            return JSONResponse(status_code=422, content={"error": {"code": "CONTRACT_INVALID", "message": f"missing fields: {missing}"}})
        registry = getattr(application.state, "skill_registry", None)
        if registry is None:
            registry = {}
            application.state.skill_registry = registry
        record = {
            "name": body["name"],
            "version": body["version"],
            "entrypoint": body["entrypoint"],
            "permissions": list(body.get("permissions", ())),
        }
        registry[record["name"]] = record
        return record

    # ------------------------------------------------- GSM8K 基准（测试集管理 + 跑测）

    @application.get("/api/v1/benchmarks/gsm8k")
    def gsm8k_overview():
        from motte_contracts import suites as contract_suites
        from motte_contracts.gsm8k import SUITE, scope_of

        presets = []
        scenarios = [s for s in resources.scenarios.list() if contract_suites.suite_of(s) == SUITE]
        for scenario in scenarios:
            dataset_name, _, dataset_version = str(scenario.get("dataset", "")).rpartition("@")
            dataset = resources.datasets.get(dataset_name, dataset_version)
            if dataset is None:
                continue
            runs = [run for run in service.store.runs.list()
                    if run.get("scenario_version") == f"{scenario['name']}@{scenario['version']}"]
            runs.sort(key=lambda run: run.get("created_at") or "", reverse=True)
            presets.append({
                "scenario": f"{scenario['name']}@{scenario['version']}",
                "dataset": scenario["dataset"],
                "scope": scope_of(dataset),
                "benchmark": dataset.get("benchmark"),
                "provenance": {k: v for k, v in dataset.get("provenance", {}).items() if k != "synthetic"}
                              | {"synthetic": dataset.get("provenance", {}).get("synthetic", False)},
                "cases": len(dataset.get("cases", ())),
                "runs": [{"id": run["id"], "status": run["status"], "created_at": run.get("created_at"),
                          "accuracy": _gsm8k_accuracy(run, service.get_run(run["id"]).get("scores", []))}
                         for run in runs[:10]],
            })
        return {"items": presets, "total": len(presets)}

    def _invalid(message: str) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": {"code": "CONTRACT_INVALID",
                                                                "message": message}})

    @application.post("/api/v1/benchmarks/gsm8k/import", status_code=201)
    def gsm8k_import(body: dict):
        """从官方仓库下载 test split 并导入，默认「最新全量」。

        省略 revision 时先解析官方仓库中该数据文件的最新 commit，再按该固定 revision 下载并
        落盘（provenance 记录的就是解析出的 sha，不是浮动分支）。scope=full 取整个 split，
        scope=smoke 取前 20 题，题数记入不可变数据集。省略 version 时自动选版本：同内容复用
        （重复导入幂等），否则取下一个空号。同 name@version 内容不同返回 409。
        """
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        from motte_contracts.gsm8k import preset_for
        from motte_sdk.benchmark import import_benchmark_split
        from motte_sdk.gsm8k_source import (SourceUnavailable, fetch_official_jsonl,
                                            latest_revision, store_source_file)
        from motte_storage.resource_store import ResourceConflictError

        revision, license_id = body.get("revision"), body.get("license")
        scope = body.get("scope") or "full"
        if revision is not None and not isinstance(revision, str):
            return _invalid("revision must be a 40-character commit hash string")
        if not isinstance(license_id, str) or not license_id:
            return _invalid("license is required")
        try:
            preset_for(scope)
        except ValueError as error:
            return _invalid(str(error))
        name = body.get("name") or "gsm8k-test"
        version = str(body.get("version") or "").strip() or None
        try:
            revision = (revision or "").strip() or latest_revision()
            raw = fetch_official_jsonl(revision)
        except ValueError as error:
            return _invalid(str(error))
        except SourceUnavailable as error:
            return JSONResponse(status_code=502, content={"error": {"code": "SOURCE_UNAVAILABLE",
                                                                    "message": str(error)}})
        store_source_file(raw, revision=revision)
        try:
            return import_benchmark_split(raw, name=name, version=version, revision=revision,
                                          license_id=license_id, scope=scope, resources=resources)
        except ValueError as error:
            code = "RESOURCE_CONFLICT" if isinstance(error, ResourceConflictError) else "CONTRACT_INVALID"
            status = 409 if isinstance(error, ResourceConflictError) else 422
            return JSONResponse(status_code=status, content={"error": {"code": code, "message": str(error)}})

    @application.get("/api/v1/benchmarks/gsm8k/cases")
    def gsm8k_cases(dataset: str, offset: int = 0, limit: int = 50, query: str = ""):
        """分页浏览某个数据集版本的题目（控制台「题目」页用）：题面、期望答案、源文件行号。

        数据集本身不可变，这里只读；运行级的题目子集由跑测接口的 case_selection 决定。
        """
        name, _, version = str(dataset).rpartition("@")
        record = resources.datasets.get(name, version)
        if record is None:
            return JSONResponse(status_code=404, content={"error": {
                "code": "DATASET_NOT_FOUND", "message": f"dataset not found: {dataset}"}})
        from motte_contracts.gsm8k import SUITE

        mismatch = _suite_mismatch(record, SUITE, dataset)
        if mismatch is not None:
            return mismatch
        cases = record.get("cases") or []
        needle = (query or "").strip().lower()
        matched = [case for case in cases
                   if not needle or needle in str(case.get("input", "")).lower()
                   or needle in str(case.get("case_id", "")).lower()]
        start = max(0, offset)
        size = min(max(1, limit), 200)
        items = [{"case_id": case["case_id"], "input": case["input"], "expected": case["expected"],
                  "source_line": (case.get("metadata") or {}).get("source_line")}
                 for case in matched[start:start + size]]
        return {"dataset": dataset, "total": len(matched), "dataset_total": len(cases),
                "offset": start, "limit": size, "query": query or "", "items": items}

    @application.post("/api/v1/benchmarks/gsm8k/runs", status_code=202)
    def gsm8k_run(body: dict):
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        from motte_contracts.gsm8k import CASE_SELECTION_KEY
        # 省略 scenario 时默认全量数据集（与导入的默认 scope 一致）；控制台始终显式传场景。
        scenario = body.get("scenario") or (
            f"{(body.get('dataset_name') or 'gsm8k-test')}-{body.get('scope') or 'full'}"
            f"@{body.get('dataset_version') or '1'}")
        scenario_name, _, scenario_version = scenario.rpartition("@")
        scenario_record = resources.scenarios.get(scenario_name, scenario_version)
        if scenario_record is None:
            # 缺失场景必须显式失败：否则 prepare_run 会退化成一条无题的通用 run。
            return JSONResponse(status_code=422, content={"error": {"code": "SCENARIO_NOT_FOUND",
                                                                    "message": f"benchmark scenario not found: {scenario}"}})
        from motte_contracts.gsm8k import SUITE

        mismatch = _suite_mismatch(scenario_record, SUITE, scenario)
        if mismatch is not None:
            return mismatch
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return JSONResponse(status_code=422, content={"error": {"code": "MODEL_REQUIRED",
                                                                    "message": "model profile id is required"}})
        manifest: dict[str, Any] = {"model": model}
        parameters = body.get("parameters")
        if parameters is not None:
            if not isinstance(parameters, dict):
                return JSONResponse(status_code=422, content={"error": {"code": "CONTRACT_INVALID",
                                                                        "message": "parameters must be an object"}})
            manifest["parameters"] = parameters
        reasoning_level = body.get("reasoning_level")
        if reasoning_level is not None:
            if not isinstance(reasoning_level, str) or not reasoning_level:
                return _invalid("reasoning_level must be a non-empty string")
            manifest["reasoning_level"] = reasoning_level
        if body.get(CASE_SELECTION_KEY) is not None:
            manifest[CASE_SELECTION_KEY] = body[CASE_SELECTION_KEY]
        try:
            prepared, case_ids = prepare_run(scenario, manifest, [], resources)
        except ManifestResolutionError as error:
            return JSONResponse(status_code=422, content={"error": {"code": error.code, "message": str(error)}})
        return service.create_run(
            scenario, prepared, case_ids, requested_manifest=manifest
        )

    # ------------------------------------------- Direct LLM 评测（通用直连 + 数据集管理）

    @application.get("/api/v1/benchmarks/direct-llm")
    def direct_llm_overview():
        from motte_contracts.direct_llm import EVAL_KEY, SUITE, is_scenario

        presets = []
        scenarios = [s for s in resources.scenarios.list() if is_scenario(s)]
        for scenario in sorted(scenarios, key=lambda s: str(s.get("name"))):
            dataset_name, _, dataset_version = str(scenario.get("dataset", "")).rpartition("@")
            dataset = resources.datasets.get(dataset_name, dataset_version)
            if dataset is None:
                continue
            runs = [run for run in service.store.runs.list()
                    if run.get("scenario_version") == f"{scenario['name']}@{scenario['version']}"]
            runs.sort(key=lambda run: run.get("created_at") or "", reverse=True)
            presets.append({
                "scenario": f"{scenario['name']}@{scenario['version']}",
                "dataset": scenario["dataset"],
                "suite": SUITE,
                "eval": dataset.get(EVAL_KEY),
                "provenance": dict(dataset.get("provenance") or {}),
                "cases": len(dataset.get("cases", ())),
                "runs": [{"id": run["id"], "status": run["status"], "created_at": run.get("created_at"),
                          "accuracy": _direct_llm_accuracy(run, service.get_run(run["id"]).get("scores", []))}
                         for run in runs[:10]],
            })
        return {"items": presets, "total": len(presets)}

    @application.get("/api/v1/benchmarks/direct-llm/builtins")
    def direct_llm_builtins():
        """仓库内置样例数据集清单（含实际题数），供控制台一键导入。"""
        from motte_sdk.direct_llm import builtin_catalog

        items = builtin_catalog()
        return {"items": items, "total": len(items)}

    @application.post("/api/v1/benchmarks/direct-llm/import", status_code=201)
    def direct_llm_import(body: dict):
        """导入一份 Direct LLM JSONL（内置样例或本地内容），落成不可变数据集 + 场景。

        ``content`` 与 ``builtin`` 二选一：前者是 UTF-8 JSONL 正文（Web 上传/粘贴、CLI 走 --file
        时由调用方读文件后传入），后者是内置样例 id（数据集名与评分器默认取内置注册表）。
        省略 version 时自动选版本：同内容复用（重复导入幂等），否则取下一个空号。
        同 name@version 内容不同返回 409。可选字段「显式传入就必须合法」——空串不会被当成默认值。
        """
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        from motte_sdk.direct_llm import (BUILTIN_LICENSE, BuiltinUnavailable,
                                          import_builtin_dataset, import_direct_llm_split)

        content, builtin = body.get("content"), body.get("builtin")
        if (content is None) == (builtin is None):
            return _invalid("exactly one of content (JSONL text) or builtin (dataset id) is required")
        if content is not None and not isinstance(content, str):
            return _invalid("content must be a JSONL string")

        def _text(key: str, default: str | None = None) -> str | None:
            """缺席才用默认值；显式传入的空串/非字符串一律拒绝（静默兜底会掩盖客户端 bug）。"""
            value = body.get(key)
            if value is None:
                return default
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")
            return value.strip()

        # version 是唯一「留空=自动」有明确语义的字段（与 GSM8K 导入一致），因此空串合法。
        raw_version = body.get("version")
        if raw_version is not None and not isinstance(raw_version, str):
            return _invalid("version must be a string")
        version = (raw_version or "").strip() or None
        try:
            # 三个字段的默认值都非空，因此 `or ""` 只用于收窄类型，不会真的兜底成空串。
            name = _text("name", builtin or "direct-llm-custom") or ""
            license_id = _text("license", BUILTIN_LICENSE) or ""
            scorer = _text("scorer")
            source = _text("source")
        except ValueError as error:
            return _invalid(str(error))
        try:
            if builtin is not None:
                if not isinstance(builtin, str) or not builtin.strip():
                    return _invalid("builtin must be a builtin dataset id")
                return import_builtin_dataset(builtin.strip(), resources=resources, name=name,
                                              version=version, license_id=license_id,
                                              scorer=scorer)
            return import_direct_llm_split(content.encode("utf-8"), name=name, version=version,
                                           license_id=license_id, scorer=scorer,
                                           source=source or "local-jsonl", resources=resources)
        except BuiltinUnavailable as error:
            return JSONResponse(status_code=404, content={"error": {"code": "BUILTIN_UNAVAILABLE",
                                                                     "message": str(error)}})
        except ValueError as error:
            code = "RESOURCE_CONFLICT" if isinstance(error, ResourceConflictError) else "CONTRACT_INVALID"
            status = 409 if isinstance(error, ResourceConflictError) else 422
            return JSONResponse(status_code=status, content={"error": {"code": code, "message": str(error)}})

    @application.get("/api/v1/benchmarks/direct-llm/cases")
    def direct_llm_cases(dataset: str, offset: int = 0, limit: int = 50, query: str = ""):
        """分页浏览某个数据集的题目（控制台「题目」页用）：题面、期望答案、生效评分器、源文件行号。"""
        from motte_contracts.direct_llm import effective_scorers

        name, _, version = str(dataset).rpartition("@")
        record = resources.datasets.get(name, version)
        if record is None:
            return JSONResponse(status_code=404, content={"error": {
                "code": "DATASET_NOT_FOUND", "message": f"dataset not found: {dataset}"}})
        from motte_contracts.direct_llm import SUITE

        mismatch = _suite_mismatch(record, SUITE, dataset)
        if mismatch is not None:
            return mismatch
        cases = record.get("cases") or []
        scorers = effective_scorers(record)
        needle = (query or "").strip().lower()
        matched = [case for case in cases
                   if not needle or needle in str(case.get("input", "")).lower()
                   or needle in str(case.get("case_id", "")).lower()]
        start = max(0, offset)
        size = min(max(1, limit), 200)
        items = [{"case_id": case["case_id"], "input": case["input"],
                  "expected": case.get("expected"), "scorer": scorers.get(case["case_id"]),
                  "source_line": (case.get("metadata") or {}).get("source_line")}
                 for case in matched[start:start + size]]
        return {"dataset": dataset, "total": len(matched), "dataset_total": len(cases),
                "offset": start, "limit": size, "query": query or "", "items": items}

    @application.post("/api/v1/benchmarks/direct-llm/runs", status_code=202)
    def direct_llm_run(body: dict):
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        from motte_contracts.selection import CASE_SELECTION_KEY

        # 省略 scenario 时按数据集名@版本推导（场景名与数据集名一致，见 direct_llm.scenario_for）。
        scenario = body.get("scenario") or (
            f"{body.get('dataset_name') or 'direct-llm-custom'}@{body.get('dataset_version') or '1'}")
        scenario_name, _, scenario_version = scenario.rpartition("@")
        scenario_record = resources.scenarios.get(scenario_name, scenario_version)
        if scenario_record is None:
            # 缺失场景必须显式失败：否则 prepare_run 会退化成一条无题的通用 run。
            return JSONResponse(status_code=422, content={"error": {"code": "SCENARIO_NOT_FOUND",
                                                                    "message": f"direct-llm scenario not found: {scenario}"}})
        from motte_contracts.direct_llm import SUITE

        mismatch = _suite_mismatch(scenario_record, SUITE, scenario)
        if mismatch is not None:
            return mismatch
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return JSONResponse(status_code=422, content={"error": {"code": "MODEL_REQUIRED",
                                                                    "message": "model profile id is required"}})
        manifest: dict[str, Any] = {"model": model}
        parameters = body.get("parameters")
        if parameters is not None:
            if not isinstance(parameters, dict):
                return _invalid("parameters must be an object")
            manifest["parameters"] = parameters
        reasoning_level = body.get("reasoning_level")
        if reasoning_level is not None:
            if not isinstance(reasoning_level, str) or not reasoning_level:
                return _invalid("reasoning_level must be a non-empty string")
            manifest["reasoning_level"] = reasoning_level
        if body.get(CASE_SELECTION_KEY) is not None:
            manifest[CASE_SELECTION_KEY] = body[CASE_SELECTION_KEY]
        try:
            prepared, case_ids = prepare_run(scenario, manifest, [], resources)
        except ManifestResolutionError as error:
            return JSONResponse(status_code=422, content={"error": {"code": error.code, "message": str(error)}})
        return service.create_run(
            scenario, prepared, case_ids, requested_manifest=manifest
        )

    return application


def _gsm8k_accuracy(run: dict[str, Any], scores: list[dict[str, Any]] | None = None) -> float | None:
    """分母取运行自己选中的题数（子集运行按子集算，旧运行回落到数据集级 selected_count）。"""
    from motte_contracts.gsm8k import run_selected_count

    rows = scores if scores is not None else (run.get("scores") or [])
    selected = run_selected_count((run.get("manifest") or {}).get("benchmark_provenance"))
    if selected is None or len(rows) != selected:
        return None
    correct = sum(1 for score in rows if score.get("outcome") == "correct")
    return round(correct / selected, 4)


def _direct_llm_accuracy(run: dict[str, Any], scores: list[dict[str, Any]] | None = None) -> float | None:
    """分母取本次运行里「声明了期望」的题数（judged）：无期望的题不判对错也不进分母。"""
    from motte_contracts.selection import run_selected_count

    rows = scores if scores is not None else (run.get("scores") or [])
    selected = run_selected_count((run.get("manifest") or {}).get("benchmark_provenance"))
    if selected is None or len(rows) != selected:
        return None
    judged = sum(1 for score in rows if score.get("judged"))
    if judged == 0:
        return None
    correct = sum(1 for score in rows if score.get("outcome") == "correct")
    return round(correct / judged, 4)


def _run_model_label(run: dict[str, Any]) -> str | None:
    """列表项模型摘要：模型档案引用 > 展开快照 > inline provider 字段。"""
    manifest = run.get("manifest") or {}
    if isinstance(manifest.get("model"), str):
        return manifest["model"]
    provider = manifest.get("provider")
    if isinstance(provider, dict) and isinstance(provider.get("model"), str):
        return provider["model"]
    return None


def _build_report(run: dict[str, Any]) -> dict[str, Any]:
    cases = run.get("cases", [])
    scores = run.get("scores", [])
    benchmark = run.get("manifest", {}).get("benchmark_provenance")
    costs = [
        case["result"]["cost"]
        for case in cases
        if isinstance(case.get("result"), dict) and isinstance(case["result"].get("cost"), dict)
    ]
    known_costs = [cost["total"] for cost in costs if cost.get("total") is not None]
    total_cost = sum(known_costs)
    versions = sorted({cost["price_table_version"] for cost in costs if cost.get("price_table_version")})
    scoring_pass_id = run.get("current_scoring_pass_id")
    passed = sum(1 for score in scores if score.get("passed") is True)
    failed = sum(1 for score in scores if score.get("passed") is False)
    judged = passed + failed
    report = {
        "schema_version": 2 if scoring_pass_id else 1,
        "run_id": run["id"],
        "scenario_version": run.get("scenario_version"),
        "status": run["status"],
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "cases": len(cases),
            "scored": len(scores),
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / judged, 4) if judged else None,
            **({"unjudged": len(scores) - judged} if len(scores) != judged else {}),
        },
        "cost": {
            "total": round(total_cost, 8) if known_costs else None,
            "price_table_versions": versions,
        },
        "scoring_pass_id": scoring_pass_id,
        "scores": scores,
        "cases": [{"case_id": case["case_id"], "result": case.get("result")} for case in cases],
    }
    if benchmark:
        report["benchmark"] = benchmark
        scoring_pass = run.get("scoring_pass") or {}
        pass_summary = scoring_pass.get("summary") or {}
        aggregate = pass_summary.get("aggregate")
        if not isinstance(aggregate, dict):
            # Legacy passes have no aggregate snapshot; retain their persisted scalar summary
            # rather than invoking mutable plugin code while serving a report.
            aggregate = {
                key: value for key, value in pass_summary.items()
                if key not in {"scores", "passed", "aggregate"}
            }
        report["summary"].update({
            key: value for key, value in aggregate.items()
            if key in ReportSummary.model_fields and key != "aggregate"
        })
        report["summary"]["aggregate"] = aggregate
        selected = aggregate.get("selected")
        responded = aggregate.get("responded")
        correct = aggregate.get("correct")
        attempted = aggregate.get("attempted")
        accuracy = aggregate.get("accuracy")
        if isinstance(selected, int):
            report["summary"]["cases"] = selected
        if isinstance(responded, int):
            report["summary"]["scored"] = responded
        if isinstance(correct, int):
            report["summary"]["passed"] = correct
            if isinstance(selected, int):
                report["summary"]["failed"] = selected - correct
        if isinstance(accuracy, (int, float)):
            report["summary"]["pass_rate"] = accuracy
        report["cost"].update(
            known_cases=len(known_costs),
            unknown_cases=(attempted - len(known_costs)) if isinstance(attempted, int) else None,
        )
        usage = [c["result"].get("usage", {}) for c in cases if isinstance(c.get("result"), dict)]
        report["usage"] = {key: sum(u[key] for u in usage if key in u)
                           for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                           if any(key in u for u in usage)}
    return report


app = create_app()
