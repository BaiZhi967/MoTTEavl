from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from motte_sdk.replay_run import ReplayProvider
from motte_sdk.service import RunService, build_run_service
from motte_storage.factory import create_resource_store
from motte_storage.resource_store import InMemoryResourceStore

SSE_POLL_INTERVAL_SECONDS = 1.0

AGENT_CATALOG = [
    {"id": "builtin-react", "kind": "react", "description": "内置 ReAct agent（工具调用 + 步数预算）"},
    {"id": "pi", "kind": "bridge", "description": "Pi Agent 桥接（bridges/pi，协议 v1）"},
]

HARNESS_CATALOG = ["claude", "codex"]
BUILTIN_SCENARIOS = {"replay@1", "json_extract@1", "direct-llm@1", "vision@1"}

_FORBIDDEN_SECRET_FIELDS = ("api_key", "key", "token", "secret", "password", "authorization")


def _secret_paths(value: Any, path: str = "$") -> list[str]:
    """Find credential-shaped keys at every nesting level before persistence."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in {
                "api_key", "api-key", "x-api-key", "authorization", "password", "token",
                "secret", "cookie", "set-cookie", "proxy-authorization",
            }:
                found.append(f"{path}.{key_text}")
            found.extend(_secret_paths(child, f"{path}.{key_text}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_secret_paths(child, f"{path}[{index}]"))
    return found


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
    leaked = _secret_paths(body)
    if leaked:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "CREDENTIALS_REJECTED",
                    "message": f"凭据字段不接受明文存储：{leaked}；只允许 api_key_env 变量名",
                }
            },
        )
    return None


def create_app(store=None, resource_store=None) -> FastAPI:
    service = build_run_service() if store is None else RunService(store)
    resources = resource_store if resource_store is not None else (
        create_resource_store() if store is None else InMemoryResourceStore()
    )
    application = FastAPI(title="MoTTEavl API", version="0.1.0")
    application.state.run_service = service
    application.state.resource_store = resources

    # ------------------------------------------------------------------ runs

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/api/v1/runs", status_code=202)
    def create_run(body: dict):
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
        manifest = body.get("manifest") or {}
        provider_config = manifest.get("provider")
        if scenario.startswith("direct-llm@") and not provider_config:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "PROVIDER_REQUIRED",
                        "message": "direct-llm runs require manifest.provider",
                    }
                },
            )
        if provider_config:
            if isinstance(provider_config, dict):
                invalid = _validate_provider(provider_config)
                if invalid is not None:
                    return invalid
            elif isinstance(provider_config, str):
                resolved_provider = resources.providers.get(provider_config)
                if resolved_provider is None:
                    return JSONResponse(
                        status_code=422,
                        content={
                            "error": {
                                "code": "RESOURCE_NOT_FOUND",
                                "message": f"provider not found: {provider_config}",
                            }
                        },
                    )
                rejected = _reject_secret_fields(resolved_provider)
                if rejected is not None:
                    return rejected
                invalid = _validate_provider(resolved_provider)
                if invalid is not None:
                    return invalid
                manifest = {**manifest, "provider": resolved_provider}
            else:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "PROVIDER_CONFIG_INVALID",
                            "message": "manifest.provider must be an object or resource name",
                        }
                    },
                )
        return service.create_run(scenario, manifest, body.get("case_ids", []))

    @application.get("/api/v1/runs")
    def list_runs(status: str | None = None):
        runs = service.store.runs.list()
        if status is not None:
            runs = [run for run in runs if run.get("status") == status]
        return {"items": runs, "total": len(runs)}

    @application.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str):
        try:
            return service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error

    @application.post("/api/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: str, body: dict | None = None):
        return service.cancel(run_id, reason=(body or {}).get("reason"))

    @application.post("/api/v1/runs/{run_id}/rescore")
    def rescore_run(run_id: str):
        return service.rescore(run_id)

    @application.post("/api/v1/runs/{run_id}/retry")
    def retry_run(run_id: str):
        try:
            return service.retry(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/api/v1/runs/{run_id}/messages", status_code=202)
    def send_run_message(run_id: str, body: dict):
        try:
            run = service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        if run["status"] in RunService.TERMINAL:
            raise HTTPException(status_code=409, detail="run is not active")
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "MESSAGE_INVALID", "message": "content is required"}},
            )
        service._emit(run_id, "user_message", {"content": content})
        return {"run_id": run_id, "status": "accepted"}

    @application.post("/api/v1/runs/{run_id}/replay")
    def replay_run(run_id: str, body: dict):
        fixture = body.get("cases", {})
        try:
            return service.execute(run_id, fixture.keys(), provider=ReplayProvider(fixture).invoke)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        except ValueError as error:
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
                    yield f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if service.get_run(run_id)["status"] in RunService.TERMINAL:
                    return
                yield ": ping\n\n"
                await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @application.get("/api/v1/runs/{run_id}/report")
    def run_report(run_id: str):
        try:
            run = service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        return _build_report(run)

    # ------------------------------------------------------- 资源 CRUD

    def _resource_routes(name: str, validate, *, versioned: bool):
        repository = getattr(resources, name)

        @application.get(f"/api/v1/{name}")
        def list_items():
            items = repository.list()
            return {"items": items, "total": len(items)}

        @application.post(f"/api/v1/{name}", status_code=201)
        def create_item(body: dict):
            rejected = _reject_secret_fields(body)
            if rejected is not None:
                return rejected
            invalid = validate(body)
            if invalid is not None:
                return invalid
            return repository.put(body)

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
                if not repository.delete(resource_name, version):
                    raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
                return {"deleted": f"{resource_name}@{version}"}

        else:
            path = f"/api/v1/{name}/{{key}}"

            @application.get(path)
            def get_item(key: str):
                record = repository.get(key)
                if record is None:
                    raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
                return record

            @application.delete(path)
            def delete_item(key: str):
                if not repository.delete(key):
                    raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
                return {"deleted": key}

    def _validate_provider_resource(body: dict):
        if not body.get("name"):
            return JSONResponse(status_code=422, content={"error": {"code": "PROVIDER_CONFIG_INVALID", "message": "name is required"}})
        if not body.get("kind"):
            return JSONResponse(status_code=422, content={"error": {"code": "PROVIDER_CONFIG_INVALID", "message": "kind is required"}})
        if body.get("kind") == "openai_compatible" and not body.get("base_url"):
            return JSONResponse(status_code=422, content={"error": {"code": "PROVIDER_CONFIG_INVALID", "message": "base_url is required for openai_compatible"}})
        return None

    def _validate_model(body: dict):
        from pydantic import ValidationError

        try:
            from motte_contracts.model import ModelProfile

            ModelProfile.model_validate(body)
        except ValidationError as error:
            issue = error.errors()[-1]
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID", "message": issue["msg"], "field": str(issue["loc"])}},
            )
        return None

    def _validate_named_version(body: dict):
        for field in ("name", "version"):
            if not body.get(field):
                return JSONResponse(status_code=422, content={"error": {"code": "CONTRACT_INVALID", "message": f"{field} is required"}})
        return None

    _resource_routes("providers", _validate_provider_resource, versioned=False)
    _resource_routes("models", _validate_model, versioned=False)
    _resource_routes("datasets", _validate_named_version, versioned=True)
    _resource_routes("scenarios", _validate_named_version, versioned=True)

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
        return {"items": reports, "total": len(reports)}

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

    return application


def _build_report(run: dict[str, Any]) -> dict[str, Any]:
    cases = run.get("cases", [])
    scores = run.get("scores", [])
    passed = sum(1 for score in scores if score.get("passed"))
    costs = [
        case["result"]["cost"]
        for case in cases
        if isinstance(case.get("result"), dict) and isinstance(case["result"].get("cost"), dict)
    ]
    total_cost = sum(cost["total"] for cost in costs if cost.get("total") is not None)
    versions = sorted({cost["price_table_version"] for cost in costs if cost.get("price_table_version")})
    return {
        "run_id": run["id"],
        "scenario_version": run.get("scenario_version"),
        "status": run["status"],
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "cases": len(cases),
            "scored": len(scores),
            "passed": passed,
            "failed": len(scores) - passed,
            "pass_rate": round(passed / len(scores), 4) if scores else None,
        },
        "cost": {"total": round(total_cost, 8) if total_cost else None, "price_table_versions": versions},
        "scores": scores,
        "cases": [{"case_id": case["case_id"], "result": case.get("result")} for case in cases],
    }


app = create_app()
