from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from motte_sdk.replay_run import ReplayProvider
from motte_sdk.resolve import ManifestResolutionError, find_secret_paths, resolve_manifest
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
            manifest = resolve_manifest(manifest, resources)
        except ManifestResolutionError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": error.code, "message": str(error)}},
            )
        if isinstance(manifest.get("provider"), dict):
            invalid = _validate_provider(manifest["provider"])
            if invalid is not None:
                return invalid
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

    def _resource_routes(name: str, validate, *, versioned: bool, key_is_path: bool = False):
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
                if not repository.delete(key):
                    raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
                return {"deleted": key}

    def _validate_provider_resource(body: dict):
        from motte_provider.config import validate_connection_config

        if not body.get("name"):
            return JSONResponse(status_code=422, content={"error": {"code": "PROVIDER_CONFIG_INVALID", "message": "name is required"}})
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
            normalized = profile.model_dump()
            normalized["parameters"] = profile.parameters.model_dump(exclude_none=True)
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
    PROVIDER_UPDATABLE_FIELDS = ("kind", "base_url", "credentials", "api_key_env", "enabled")
    MODEL_UPDATABLE_FIELDS = (
        "model", "enabled", "capabilities", "input_modalities", "output_modalities",
        "supports_tools", "tool_features", "context_window", "max_output_tokens",
        "reasoning", "parameters", "provenance",
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
        invalid = _validate_provider_resource(merged)
        if invalid is not None:
            return invalid
        return resources.providers.put(merged)

    @application.put("/api/v1/models/{model_id:path}")
    def update_model(model_id: str, body: dict):
        record = resources.models.get(model_id)
        if record is None:
            raise HTTPException(status_code=404, detail="model not found")
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
        }
        invalid = _validate_model(merged)
        if invalid is not None:
            return invalid
        return resources.models.put(merged)

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
            config = resolve_manifest(manifest, resources)["provider"]
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
