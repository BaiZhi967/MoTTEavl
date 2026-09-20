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
from motte_sdk.resolve import (
    ManifestResolutionError,
    find_secret_paths,
    prepare_run,
    resolve_manifest,
)
from motte_sdk.service import RunService, build_run_service
from motte_storage import RunConflictError
from motte_storage.factory import create_resource_store
from motte_storage.resource_store import InMemoryResourceStore, ResourceConflictError

from apps.api.app.schemas import (
    AgentTasksDryRunResponse,
    AgentTasksImportRequest,
    AgentTasksRunRequest,
    CancelRunRequest,
    CreateRunRequest,
    DatasetSourceListResponse,
    DatasetSummaryListResponse,
    DirectLlmDryRunResponse,
    DirectLlmImportRequest,
    DirectLlmImportResponse,
    DirectLlmOverviewResponse,
    DirectLlmRunRequest,
    ReplayRunRequest,
    ResourcePublicationListResponse,
    RunCommandListResponse,
    RunListResponse,
    RunMessageRequest,
    ScoringPassListResponse,
)

SSE_POLL_INTERVAL_SECONDS = 1.0

AGENT_CATALOG = [
    {
        "id": "builtin-react",
        "kind": "react",
        "description": "内置 ReAct agent（尚未接入 ExecutionBackend）",
        "protocol_ready": True,
        "execution_ready": False,
    },
    {
        "id": "pi",
        "kind": "bridge",
        "description": "Pi Agent bridge protocol v1",
        "protocol_ready": True,
        "execution_ready": False,
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
        content={
            "error": {
                "code": "SUITE_MISMATCH",
                "message": f"resource {reference} belongs to suite {actual!r}, expected {expected!r}",
            }
        },
    )


def _resource_hash(record: dict[str, Any]) -> str:
    ignored = {"profile_hash", "generation", "lifecycle", "published_at", "deprecated_at"}
    payload = {key: value for key, value in record.items() if key not in ignored}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dataset_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Project immutable identity and governance without returning large case payloads."""
    evaluation = record.get("eval") if isinstance(record.get("eval"), dict) else {}
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    scorer = evaluation.get("scorer")
    if isinstance(scorer, dict):
        scorer_id = scorer.get("id")
        scorer_version = scorer.get("version")
        scorer = (
            f"{scorer_id}@{scorer_version}"
            if isinstance(scorer_id, str) and isinstance(scorer_version, str)
            else None
        )
    license_info = provenance.get("license")
    license_status = license_info.get("status") if isinstance(license_info, dict) else license_info
    profiles = []
    for profile in record.get("profiles") or []:
        if not isinstance(profile, dict) or not isinstance(profile.get("name"), str):
            continue
        count = profile.get("count", len(profile.get("case_ids") or []))
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            continue
        profiles.append(
            {
                "name": profile["name"],
                "count": count,
                "strategy": profile.get("strategy")
                if isinstance(profile.get("strategy"), str)
                else None,
                "case_ids_sha256": (
                    profile.get("case_ids_sha256")
                    if isinstance(profile.get("case_ids_sha256"), str)
                    else None
                ),
            }
        )
    source = provenance.get("source_id", provenance.get("source"))
    revision = provenance.get("upstream_revision", provenance.get("revision"))
    return {
        "name": record.get("name"),
        "version": str(record.get("version", "")),
        "contract_version": record.get("contract_version", evaluation.get("version")),
        "dataset_fingerprint": record.get("dataset_fingerprint"),
        "cases": len(record.get("cases") or []),
        "cases_sha256": record.get("cases_sha256"),
        "suite": evaluation.get("suite"),
        "scorer": scorer if isinstance(scorer, str) else None,
        "source": source if isinstance(source, str) else None,
        "revision": revision if isinstance(revision, str) else None,
        "license_status": license_status if isinstance(license_status, str) else None,
        "profiles": profiles,
    }


def _load_json_file(path: str | None) -> dict | None:
    """读取可选的 Runner 预检报告；缺失/畸形按"未上报"处理（预检随后 fail closed）。"""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def create_app(store=None, resource_store=None) -> FastAPI:
    service = build_run_service() if store is None else RunService(store)
    resources = (
        resource_store
        if resource_store is not None
        else (create_resource_store() if store is None else InMemoryResourceStore())
    )
    application = FastAPI(title="MoTTEavl API", version="0.1.0")
    application.state.run_service = service
    application.state.resource_store = resources

    @application.exception_handler(ResourceConflictError)
    async def resource_conflict(request, error):
        return JSONResponse(
            status_code=409, content={"error": {"code": "RESOURCE_CONFLICT", "message": str(error)}}
        )

    @application.exception_handler(RunConflictError)
    async def run_conflict(request, error):
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "RUN_CONFLICT",
                    "message": "run revision or status changed",
                }
            },
        )

    # ------------------------------------------------------------------ runs

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post(
        "/api/v1/runs",
        status_code=202,
        response_model=Run,
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
                content={
                    "error": {
                        "code": "SCENARIO_REQUIRED",
                        "message": "scenario_version is required",
                    }
                },
            )
        if scenario.startswith("vision@"):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "MODEL_CAPABILITY_UNSUPPORTED",
                        "message": "vision capability is unsupported",
                    }
                },
            )
        if scenario not in BUILTIN_SCENARIOS:
            try:
                scenario_name, scenario_version = scenario.rsplit("@", 1)
            except ValueError:
                scenario_name, scenario_version = scenario, ""
            if (
                not scenario_name
                or resources.scenarios.get(scenario_name, scenario_version) is None
            ):
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
            manifest, case_ids = prepare_run(
                scenario, manifest, body.get("case_ids", []), resources
            )
        except ManifestResolutionError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": error.code, "message": str(error)}},
            )
        execution = manifest.get("execution") or {}
        provider = manifest.get("provider") or {}
        fixture = provider.get("fixture") if isinstance(provider, dict) else None
        if execution.get("backend_id") == "replay" and fixture is not None:
            if (
                not isinstance(fixture, dict)
                or not fixture
                or any(
                    not isinstance(case_id, str)
                    or not case_id
                    or not isinstance(item, dict)
                    or "output" not in item
                    for case_id, item in fixture.items()
                )
            ):
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "REPLAY_FIXTURE_INVALID",
                            "message": "replay fixture must contain object cases with output fields",
                        }
                    },
                )
            if not case_ids:
                case_ids = list(fixture)
            missing = [case_id for case_id in case_ids if case_id not in fixture]
            if missing:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "REPLAY_CASE_NOT_FOUND",
                            "message": f"replay fixture has no selected cases: {missing}",
                        }
                    },
                )
        if isinstance(manifest.get("provider"), dict):
            invalid = _validate_provider(manifest["provider"])
            if invalid is not None:
                return invalid
        return service.create_run(
            scenario, manifest, case_ids, requested_manifest=requested_manifest
        )

    @application.get(
        "/api/v1/runs",
        response_model=RunListResponse,
        response_model_exclude_unset=True,
    )
    def list_runs(status: str | None = None):
        runs = service.store.runs.list()
        if status is not None:
            runs = [run for run in runs if run.get("status") == status]
        items = [adapt_legacy_run({**run, "model": _run_model_label(run)}) for run in runs]
        return {"items": items, "total": len(items)}

    @application.get(
        "/api/v1/runs/{run_id}",
        response_model=Run,
        response_model_exclude_unset=True,
    )
    def get_run(run_id: str):
        try:
            return adapt_legacy_run(_redact_agent_run_view(service.get_run(run_id)))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error

    @application.post(
        "/api/v1/runs/{run_id}/cancel",
        response_model=Run,
        response_model_exclude_unset=True,
    )
    def cancel_run(run_id: str, body: CancelRunRequest | None = None):
        try:
            # R3 #4：所有返回 Run 视图的公共端点统一脱敏（与 GET /runs/{id} 一致）
            return adapt_legacy_run(_redact_agent_run_view(
                service.cancel(run_id, reason=body.reason if body is not None else None)
            ))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/runs/{run_id}/rescore",
        response_model=Run,
        response_model_exclude_unset=True,
    )
    def rescore_run(run_id: str):
        try:
            return adapt_legacy_run(_redact_agent_run_view(service.rescore(run_id)))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/runs/{run_id}/retry",
        response_model=Run,
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
                        if pinned_selection.get("mode") == "profile":
                            profile_name = pinned_selection.get("profile")
                            if not isinstance(profile_name, str) or not profile_name.strip():
                                raise ValueError("profile_stale retry has an invalid pinned profile")
                            requested.pop("profile", None)
                            requested["case_selection"] = {
                                "mode": "profile", "profile": profile_name,
                            }
                        else:
                            requested["case_selection"] = deepcopy(pinned_selection)
                    selected = []
                else:
                    selected = parent.get("case_ids") or []
                refreshed, refreshed_case_ids = prepare_run(
                    parent["scenario_version"], requested, selected, resources
                )
            elif parent["status"] in {"failed", "cancelled", "unsupported", "needs_review"}:
                managed = (parent.get("manifest") or {}).get("benchmark_provenance") or {}
                if managed.get("plugin_version") == "2":
                    from motte_sdk.direct_llm_v2 import require_runnable_direct_llm_v2_snapshot

                    require_runnable_direct_llm_v2_snapshot(parent, resources)
            return adapt_legacy_run(_redact_agent_run_view(service.retry(
                run_id, refreshed_manifest=refreshed, case_ids=refreshed_case_ids
            )))
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
                content={
                    "error": {
                        "code": "COMMAND_UNSUPPORTED",
                        "message": "the run execution backend is not interactive",
                    }
                },
            )
        return JSONResponse(
            status_code=501,
            content={
                "error": {
                    "code": "RUN_COMMANDS_NOT_IMPLEMENTED",
                    "message": "no registered execution backend has a durable command consumer",
                }
            },
        )

    @application.get("/api/v1/runs/{run_id}/commands", response_model=RunCommandListResponse)
    def list_run_commands(run_id: str):
        try:
            service.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        items = service.store.commands.list_for_run(run_id)
        return {"items": items, "total": len(items)}

    @application.post(
        "/api/v1/runs/{run_id}/replay",
        status_code=202,
        response_model=Run,
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
            has_provider_fixture = (
                isinstance(existing_provider, dict) and "fixture" in existing_provider
            )
            has_manifest_fixture = "replay_fixture" in manifest
            manifest_fixture = manifest.get("replay_fixture") if has_manifest_fixture else None
            if (
                has_provider_fixture
                and has_manifest_fixture
                and provider_fixture != manifest_fixture
            ):
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
            run.update(
                {
                    "manifest": manifest,
                    "requested_manifest": requested,
                    "case_ids": selected,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            service.store.runs.update(
                run,
                expected_revision=run["revision"],
                expected_status="queued",
            )
            return adapt_legacy_run(_redact_agent_run_view(service.get_run(run_id)))
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
            from motte_trace.redaction import redact_secrets

            while True:
                for event in service.events_after(run_id, cursor):
                    cursor = event["seq"]
                    # R3 #4：SSE 公共流按值形状脱敏；持久 trace 原文不动
                    envelope = TraceEvent.model_validate(
                        adapt_legacy_trace_event(redact_secrets(event))
                    ).model_dump(mode="json", exclude_none=True)
                    yield f"id: {event['seq']}\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"
                if service.get_run(run_id)["status"] in RunService.TERMINAL:
                    return
                yield ": ping\n\n"
                await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @application.get(
        "/api/v1/runs/{run_id}/report",
        response_model=RunReport,
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
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": {
                            "code": "SCORING_PASS_NOT_FOUND",
                            "message": f"scoring pass not found for run: {scoring_pass_id}",
                        }
                    },
                )
            run["scores"] = service.store.score_sets.list_for_pass(scoring_pass_id)
            run["current_scoring_pass_id"] = scoring_pass_id
            run["scoring_pass"] = selected
        return adapt_legacy_report(_build_report(adapt_legacy_run(_redact_agent_run_view(run))))

    @application.get("/api/v1/runs/{run_id}/scoring-passes", response_model=ScoringPassListResponse)
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

        @application.get(
            f"/api/v1/{name}",
            response_model=DatasetSummaryListResponse if name == "datasets" else None,
        )
        def list_items():
            records = repository.list()
            items = (
                [_dataset_summary(record) for record in records] if name == "datasets" else records
            )
            return {"items": items, "total": len(items)}

        @application.post(f"/api/v1/{name}", status_code=201)
        def create_item(body: dict):
            server_managed = {"_deleted"} | {
                "providers": {"generation"},
                "models": {
                    "generation",
                    "lifecycle",
                    "profile_hash",
                    "published_at",
                    "deprecated_at",
                },
            }.get(name, set())
            supplied_managed = sorted(set(body).intersection(server_managed))
            if supplied_managed:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "SERVER_MANAGED_FIELD",
                            "message": f"server-managed fields are not accepted: {supplied_managed}",
                        }
                    },
                )
            rejected = _reject_secret_fields(body)
            if rejected is not None:
                return rejected
            evaluation = body.get("eval")
            if (
                name in {"datasets", "scenarios"}
                and isinstance(evaluation, dict)
                and evaluation.get("suite") == "direct-llm"
                and evaluation.get("version") == 2
            ):
                return JSONResponse(
                    status_code=422,
                    content={"error": {
                        "code": "ATOMIC_PUBLICATION_REQUIRED",
                        "message": (
                            "direct-llm v2 datasets and scenarios must be published atomically "
                            "with a matching publication audit"
                        ),
                    }},
                )
            invalid = validate(body)
            if invalid is not None:
                return invalid
            if not versioned:
                resource_key = body.get("name") if name == "providers" else body.get("id")
                if resource_key and repository.get(resource_key) is not None:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": {
                                "code": "RESOURCE_CONFLICT",
                                "message": f"{name[:-1]} already exists: {resource_key}",
                            }
                        },
                    )
            try:
                return repository.put(body, expected_generation=0 if not versioned else None)
            except ResourceConflictError:
                raise
            except ValueError as error:
                return JSONResponse(
                    status_code=422,
                    content={"error": {"code": "CONTRACT_INVALID", "message": str(error)}},
                )

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
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": {
                            "code": "PUBLISHED_RESOURCE_IMMUTABLE",
                            "message": f"published resource cannot be deleted: {resource_name}@{version}",
                        }
                    },
                )

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
                    repository.put(deprecated, expected_generation=int(record.get("generation", 1)))
                    return {"deprecated": key}
                if not repository.delete(key, expected_generation=int(record.get("generation", 1))):
                    raise HTTPException(status_code=404, detail=f"{name[:-1]} not found")
                return {"deleted": key}

    def _validate_provider_resource(body: dict):
        from motte_provider.config import validate_connection_config

        if not body.get("name"):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {"code": "PROVIDER_CONFIG_INVALID", "message": "name is required"}
                },
            )
        body.setdefault("generation", 1)
        try:
            validate_connection_config(body)
        except ValueError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "PROVIDER_CONFIG_INVALID", "message": str(error)}},
            )
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
                content={
                    "error": {
                        "code": "CONTRACT_INVALID",
                        "message": issue["msg"],
                        "field": str(issue["loc"]),
                    }
                },
            )
        provider_name = body.get("provider")
        if provider_name and resources.providers.get(provider_name) is None:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "RESOURCE_NOT_FOUND",
                        "message": f"provider not found: {provider_name}",
                    }
                },
            )
        return None

    def _validate_named_version(body: dict):
        for field in ("name", "version"):
            if not body.get(field):
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {"code": "CONTRACT_INVALID", "message": f"{field} is required"}
                    },
                )
        return None

    def _validate_price_table(body: dict):
        from motte_provider.pricing import parse_price_table

        for field in ("model_id", "version"):
            if not body.get(field):
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {"code": "CONTRACT_INVALID", "message": f"{field} is required"}
                    },
                )
        try:
            parse_price_table(body)
        except ValueError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID", "message": str(error)}},
            )
        return None

    _resource_routes("providers", _validate_provider_resource, versioned=False, key_is_path=True)
    _resource_routes("models", _validate_model, versioned=False, key_is_path=True)
    _resource_routes("datasets", _validate_named_version, versioned=True)
    _resource_routes("scenarios", _validate_named_version, versioned=True)
    _resource_routes("price_tables", _validate_price_table, versioned=True)

    @application.get(
        "/api/v1/resource-publications", response_model=ResourcePublicationListResponse
    )
    def list_resource_publications():
        items = resources.publications.list()
        return {"items": items, "total": len(items)}

    # 读-改-写更新：只接受白名单字段，其余载荷原样保留（put 是整记录覆盖，先合再校验）
    PROVIDER_UPDATABLE_FIELDS = (
        "kind",
        "base_url",
        "credentials",
        "api_key_env",
        "enabled",
        "request_path",
        "timeout",
        "max_retries",
        "backoff_initial_ms",
        "backoff_max_ms",
    )
    MODEL_UPDATABLE_FIELDS = (
        "model",
        "enabled",
        "capabilities",
        "input_modalities",
        "output_modalities",
        "supports_tools",
        "tool_features",
        "context_window",
        "max_output_tokens",
        "reasoning",
        "parameters",
        "provenance",
        "identity_policy",
        "identity_aliases",
        "identity_alias_version",
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
                content={
                    "error": {"code": "CONTRACT_INVALID", "message": "provider name is immutable"}
                },
            )
        unknown = sorted(set(body) - set(PROVIDER_UPDATABLE_FIELDS) - {"name"})
        if unknown:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {"code": "CONTRACT_INVALID", "message": f"unknown fields: {unknown}"}
                },
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
        return resources.providers.put(merged, expected_generation=int(record.get("generation", 1)))

    @application.put("/api/v1/models/{model_id:path}")
    def update_model(model_id: str, body: dict):
        record = resources.models.get(model_id)
        if record is None:
            raise HTTPException(status_code=404, detail="model not found")
        if record.get("lifecycle", "draft") != "draft":
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "PUBLISHED_RESOURCE_IMMUTABLE",
                        "message": "published or deprecated model profiles cannot be updated",
                    }
                },
            )
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
                content={
                    "error": {"code": "CONTRACT_INVALID", "message": "model provider is immutable"}
                },
            )
        unknown = sorted(set(body) - set(MODEL_UPDATABLE_FIELDS) - {"id", "provider"})
        if unknown:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {"code": "CONTRACT_INVALID", "message": f"unknown fields: {unknown}"}
                },
            )
        merged = {
            **record,
            **{key: body[key] for key in MODEL_UPDATABLE_FIELDS if key in body},
            "generation": int(record.get("generation", 1)) + 1,
        }
        invalid = _validate_model(merged)
        if invalid is not None:
            return invalid
        return resources.models.put(merged, expected_generation=int(record.get("generation", 1)))

    @application.post("/api/v1/models/{model_id:path}/publish")
    def publish_model(model_id: str):
        record = resources.models.get(model_id)
        if record is None:
            raise HTTPException(status_code=404, detail="model not found")
        lifecycle = record.get("lifecycle", "draft")
        if lifecycle == "published":
            return record
        if lifecycle != "draft":
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "RESOURCE_LIFECYCLE_INVALID",
                        "message": f"model cannot be published from lifecycle {lifecycle!r}",
                    }
                },
            )
        published = {
            **record,
            "lifecycle": "published",
            "published_at": datetime.now(UTC).isoformat(),
            "generation": int(record.get("generation", 1)) + 1,
        }
        invalid = _validate_model(published)
        if invalid is not None:
            return invalid
        return resources.models.put(published, expected_generation=int(record.get("generation", 1)))

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
                content={
                    "error": {
                        "code": "RESOURCE_NOT_FOUND",
                        "message": f"provider not found: {profile.get('provider')}",
                    }
                },
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
                content={
                    "error": {
                        "code": "PROVIDER_TEST_UNSUPPORTED",
                        "message": f"kind 不支持测试调用: {connection.get('kind')}",
                    }
                },
            )
        from motte_provider.config import build_case_provider

        try:
            manifest = {"model": model_id}
            if "reasoning_level" in (body or {}):
                manifest["reasoning_level"] = body["reasoning_level"]
            config = resolve_manifest(manifest, resources, allow_draft_model=True)["provider"]
            # Use the same snapshot/factory as real runs, but never exceed 16 output tokens.
            bound = min(16, config.get("max_output_tokens") or 16)
            config["parameters"] = {**(config.get("parameters") or {}), "max_output_tokens": bound}
            config["max_output_tokens"] = bound
            provider = build_case_provider(config).provider
            model = provider.model
            request = ModelRequest(
                model=model,
                messages=[
                    Message(
                        role="user",
                        content=(body or {}).get("prompt") or "Reply with exactly: pong",
                    )
                ],
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
        items = [{**report, "protocol_ready": True, "execution_ready": False} for report in reports]
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
            return JSONResponse(
                status_code=422,
                content={
                    "error": {"code": "CONTRACT_INVALID", "message": f"missing fields: {missing}"}
                },
            )
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
            runs = [
                run
                for run in service.store.runs.list()
                if run.get("scenario_version") == f"{scenario['name']}@{scenario['version']}"
            ]
            runs.sort(key=lambda run: run.get("created_at") or "", reverse=True)
            presets.append(
                {
                    "scenario": f"{scenario['name']}@{scenario['version']}",
                    "dataset": scenario["dataset"],
                    "scope": scope_of(dataset),
                    "benchmark": dataset.get("benchmark"),
                    "provenance": {
                        k: v for k, v in dataset.get("provenance", {}).items() if k != "synthetic"
                    }
                    | {"synthetic": dataset.get("provenance", {}).get("synthetic", False)},
                    "cases": len(dataset.get("cases", ())),
                    "runs": [
                        {
                            "id": run["id"],
                            "status": run["status"],
                            "created_at": run.get("created_at"),
                            "accuracy": _gsm8k_accuracy(
                                run, service.get_run(run["id"]).get("scores", [])
                            ),
                        }
                        for run in runs[:10]
                    ],
                }
            )
        return {"items": presets, "total": len(presets)}

    def _invalid(message: str) -> JSONResponse:
        return JSONResponse(
            status_code=422, content={"error": {"code": "CONTRACT_INVALID", "message": message}}
        )

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
        from motte_sdk.gsm8k_source import (
            SourceUnavailable,
            fetch_official_jsonl,
            latest_revision,
            store_source_file,
        )
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
            return JSONResponse(
                status_code=502,
                content={"error": {"code": "SOURCE_UNAVAILABLE", "message": str(error)}},
            )
        store_source_file(raw, revision=revision)
        try:
            return import_benchmark_split(
                raw,
                name=name,
                version=version,
                revision=revision,
                license_id=license_id,
                scope=scope,
                resources=resources,
            )
        except ValueError as error:
            code = (
                "RESOURCE_CONFLICT"
                if isinstance(error, ResourceConflictError)
                else "CONTRACT_INVALID"
            )
            status = 409 if isinstance(error, ResourceConflictError) else 422
            return JSONResponse(
                status_code=status, content={"error": {"code": code, "message": str(error)}}
            )

    @application.get("/api/v1/benchmarks/gsm8k/cases")
    def gsm8k_cases(dataset: str, offset: int = 0, limit: int = 50, query: str = ""):
        """分页浏览某个数据集版本的题目（控制台「题目」页用）：题面、期望答案、源文件行号。

        数据集本身不可变，这里只读；运行级的题目子集由跑测接口的 case_selection 决定。
        """
        name, _, version = str(dataset).rpartition("@")
        record = resources.datasets.get(name, version)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "DATASET_NOT_FOUND",
                        "message": f"dataset not found: {dataset}",
                    }
                },
            )
        from motte_contracts.gsm8k import SUITE

        mismatch = _suite_mismatch(record, SUITE, dataset)
        if mismatch is not None:
            return mismatch
        cases = record.get("cases") or []
        needle = (query or "").strip().lower()
        matched = [
            case
            for case in cases
            if not needle
            or needle in str(case.get("input", "")).lower()
            or needle in str(case.get("case_id", "")).lower()
        ]
        start = max(0, offset)
        size = min(max(1, limit), 200)
        items = [
            {
                "case_id": case["case_id"],
                "input": case["input"],
                "expected": case["expected"],
                "source_line": (case.get("metadata") or {}).get("source_line"),
            }
            for case in matched[start : start + size]
        ]
        return {
            "dataset": dataset,
            "total": len(matched),
            "dataset_total": len(cases),
            "offset": start,
            "limit": size,
            "query": query or "",
            "items": items,
        }

    @application.post("/api/v1/benchmarks/gsm8k/runs", status_code=202)
    def gsm8k_run(body: dict):
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        from motte_contracts.gsm8k import CASE_SELECTION_KEY

        # 省略 scenario 时默认全量数据集（与导入的默认 scope 一致）；控制台始终显式传场景。
        scenario = body.get("scenario") or (
            f"{(body.get('dataset_name') or 'gsm8k-test')}-{body.get('scope') or 'full'}"
            f"@{body.get('dataset_version') or '1'}"
        )
        scenario_name, _, scenario_version = scenario.rpartition("@")
        scenario_record = resources.scenarios.get(scenario_name, scenario_version)
        if scenario_record is None:
            # 缺失场景必须显式失败：否则 prepare_run 会退化成一条无题的通用 run。
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "SCENARIO_NOT_FOUND",
                        "message": f"benchmark scenario not found: {scenario}",
                    }
                },
            )
        from motte_contracts.gsm8k import SUITE

        mismatch = _suite_mismatch(scenario_record, SUITE, scenario)
        if mismatch is not None:
            return mismatch
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {"code": "MODEL_REQUIRED", "message": "model profile id is required"}
                },
            )
        manifest: dict[str, Any] = {"model": model}
        parameters = body.get("parameters")
        if parameters is not None:
            if not isinstance(parameters, dict):
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "CONTRACT_INVALID",
                            "message": "parameters must be an object",
                        }
                    },
                )
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
            return JSONResponse(
                status_code=422, content={"error": {"code": error.code, "message": str(error)}}
            )
        return service.create_run(scenario, prepared, case_ids, requested_manifest=manifest)

    # ------------------------------------------- 外部 Benchmark（job-based C-Eval/CMMLU，M2-T07 + review R01/R13/R14/R16）

    from motte_benchmark.registry import registered_adapter_ids
    from motte_benchmark.runner_config import ensure_builtin_adapters
    from motte_sdk.benchmark_catalog import (
        BENCHMARK_DESCRIPTORS,
        BenchmarkCatalog,
        external_environment_digest,
        external_runner_version,
        prepare_external_dataset,
        prepare_external_run_inputs,
        validate_external_run_request,
    )

    # API/Worker/CLI 从同一受控配置加载 adapter（review R01/R13）。
    ensure_builtin_adapters()
    _EXTERNAL_BENCHMARKS = ("ceval", "cmmlu")
    external_catalog = BenchmarkCatalog(getattr(service.store, "benchmark_datasets", None))
    for _benchmark_id in _EXTERNAL_BENCHMARKS:
        external_catalog.register(
            _benchmark_id,
            benchmark_version=BENCHMARK_DESCRIPTORS[_benchmark_id].benchmark_version,
        )

    def _external_catalog_sync() -> None:
        for benchmark_id in _EXTERNAL_BENCHMARKS:
            external_catalog.mark_runner_connected(
                benchmark_id,
                BENCHMARK_DESCRIPTORS[benchmark_id].adapter_id in registered_adapter_ids(),
            )

    def _external_prepare(benchmark_id: str, body: dict):
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        raw_files = body.get("files")
        if not isinstance(raw_files, dict) or not raw_files:
            return JSONResponse(
                status_code=422,
                content={"error": {
                    "code": "CONTRACT_INVALID",
                    "message": "files must be a non-empty object of logical name to JSONL text",
                }},
            )
        files: dict[str, bytes] = {}
        for name, content in raw_files.items():
            if not isinstance(name, str) or not isinstance(content, str):
                return JSONResponse(
                    status_code=422,
                    content={"error": {
                        "code": "CONTRACT_INVALID",
                        "message": "files keys/values must be strings",
                    }},
                )
            files[name] = content.encode("utf-8")
        revision = body.get("dataset_revision")
        if not isinstance(revision, str) or not revision.strip():
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "CONTRACT_INVALID",
                                   "message": "dataset_revision is required"}},
            )
        declared = body.get("declared_sha256")
        default_split = body.get("default_split")
        prepared = prepare_external_dataset(
            files=files,
            dataset_revision=revision,
            declared_sha256=declared if isinstance(declared, dict) else None,
            provenance_target=body.get("provenance_target") or "user-supplied",
            approval_evidence=body.get("approval_evidence"),
            approval_verifier=None,
            license_evidence=body.get("license_evidence"),
            benchmark_id=benchmark_id,
            default_split=default_split if isinstance(default_split, str) else None,
        )
        if prepared.state != "ready":
            return JSONResponse(
                status_code=422,
                content={"error": {
                    "code": "DATASET_PREPARE_FAILED",
                    "message": "dataset preparation failed; see reasons",
                    "details": {"reasons": list(prepared.reasons)},
                }},
            )
        # 准备结果持久化（review R13）：重启/第二个实例不再丢失；
        # 同 revision 重写不同内容被拒（review R2-05）。
        try:
            external_catalog.update_dataset(benchmark_id, prepared)
        except ValueError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "DATASET_REVISION_REUSED", "message": str(error)}},
            )
        external_catalog.mark_profile_validated(benchmark_id, True)
        _external_catalog_sync()
        return {
            "state": prepared.state,
            "provenance": prepared.provenance,
            "revision": prepared.dataset_revision,
            "rows": prepared.row_count,
            "gold_rows": prepared.gold_count,
            "unscored": prepared.unscored,
        }

    def _external_cases_view(benchmark_id: str, offset: int, limit: int, query: str):
        dataset = external_catalog.dataset(benchmark_id)
        if dataset is None:
            return {"cases": [], "total": 0}
        entries = [
            {
                "case_id": entry.case_id, "subject": entry.subject,
                "has_gold": entry.has_gold, "split": entry.split,
            }
            for entry in dataset.manifest
            if not query or query in entry.case_id or query in entry.subject
        ]
        window = entries[offset:offset + max(1, min(limit, 200))]
        return {"cases": window, "total": len(entries)}

    def _external_run_model_record(model_id: Any):
        if not isinstance(model_id, str) or not model_id:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "MODEL_REQUIRED",
                                   "message": "model profile id is required"}},
            )
        return resources.models.get(model_id)

    def _external_preflight_view(benchmark_id: str, params: dict):
        """GET 预检与创建共用同一校验服务/同一输入（review R14）。"""
        from motte_sdk.context_preflight import external_benchmark_preflight

        _external_catalog_sync()
        model_id = params.get("model")
        model_record = resources.models.get(model_id) if isinstance(model_id, str) and model_id else None
        dataset = external_catalog.dataset(benchmark_id)
        descriptor = BENCHMARK_DESCRIPTORS[benchmark_id]
        split = params.get("split") or descriptor.default_split
        few_shot = int(params.get("few_shot") or 0)
        few_shot_split = params.get("few_shot_split") or descriptor.default_few_shot_split
        scope = params.get("scope") or "custom-subset"
        case_ids = params.get("case_ids")
        runner_connected = descriptor.adapter_id in registered_adapter_ids()
        reasons = validate_external_run_request(
            dataset if dataset is not None else _empty_dataset(benchmark_id),
            benchmark_id=benchmark_id,
            model_record=model_record,
            scope=scope,
            split=split,
            few_shot=few_shot,
            few_shot_split=few_shot_split,
            case_ids=case_ids if isinstance(case_ids, list) else None,
            runner_connected=runner_connected,
        )
        profile = {
            "selected_subjects": (
                sorted({entry.subject for entry in dataset.manifest})
                if dataset else []
            ),
            "split": split,
            "runner_version": external_runner_version(),
            "environment_digest": external_environment_digest(benchmark_id),
        }
        report = external_benchmark_preflight(
            profile,
            model_record if isinstance(model_record, dict) else {},
            dataset={
                "state": (dataset.state if dataset else "unprepared"),
                "subjects": sorted({entry.subject for entry in dataset.manifest}) if dataset else [],
                "split": split,
            },
            runner_connected=runner_connected,
        )
        merged = list(dict.fromkeys([*reasons, *report["reasons"]]))
        return {
            "ok": not merged,
            "reasons": merged,
            "checks": report["checks"],
        }

    def _empty_dataset(benchmark_id: str):
        from motte_sdk.benchmark_catalog import PreparedBenchmarkDataset

        return PreparedBenchmarkDataset(
            benchmark_id=benchmark_id,
            benchmark_version=BENCHMARK_DESCRIPTORS[benchmark_id].benchmark_version,
            dataset_revision="unprepared",
            state="unprepared",
        )

    def _external_run_create(benchmark_id: str, body: dict):
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        _external_catalog_sync()
        descriptor = BENCHMARK_DESCRIPTORS[benchmark_id]
        model_id = body.get("model")
        model_missing = _external_run_model_record(model_id)
        if isinstance(model_missing, JSONResponse):
            return model_missing
        model_record = model_missing
        entry = external_catalog.status(benchmark_id)
        dataset = external_catalog.dataset(benchmark_id)
        if dataset is None or dataset.state != "ready":
            return JSONResponse(
                status_code=422,
                content={"error": {
                    "code": "DATASET_UNPREPARED",
                    "message": f"{benchmark_id} dataset is not prepared",
                    "details": {"blockers": entry["blockers"]},
                }},
            )
        if descriptor.adapter_id not in registered_adapter_ids():
            return JSONResponse(
                status_code=422,
                content={"error": {
                    "code": "RUNNER_NOT_CONNECTED",
                    "message": "external benchmark adapter is not registered",
                }},
            )
        # 入队前统一预检（review R14）：生命周期/split/few-shot/scope 失败 422，0 Job。
        reasons = validate_external_run_request(
            dataset,
            benchmark_id=benchmark_id,
            model_record=model_record,
            scope=str(body.get("scope") or "custom-subset"),
            split=body.get("split") if isinstance(body.get("split"), str) else None,
            few_shot=int(body.get("few_shot") or 0),
            few_shot_split=(
                body.get("few_shot_split")
                if isinstance(body.get("few_shot_split"), str) else None
            ),
            case_ids=body.get("case_ids") if isinstance(body.get("case_ids"), list) else None,
            runner_connected=True,
        )
        if reasons:
            return JSONResponse(
                status_code=422,
                content={"error": {
                    "code": "RUN_REQUEST_INVALID",
                    "message": "run request rejected by shared validation",
                    "details": {"reasons": reasons},
                }},
            )
        credentials = body.get("credentials")
        try:
            inputs = prepare_external_run_inputs(
                dataset,
                benchmark_id=benchmark_id,
                model_id=model_id,
                model_record=model_record,
                few_shot=int(body.get("few_shot") or 0),
                seed=int(body.get("seed") or 0),
                scope=str(body.get("scope") or "custom-subset"),
                split=body.get("split") if isinstance(body.get("split"), str) else None,
                few_shot_split=(
                    body.get("few_shot_split")
                    if isinstance(body.get("few_shot_split"), str) else None
                ),
                credentials=credentials if isinstance(credentials, dict) else None,
                case_ids=body.get("case_ids") if isinstance(body.get("case_ids"), list) else None,
                resources=resources,
            )
        except ValueError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "RUN_REQUEST_INVALID", "message": str(error)}},
            )
        from motte_sdk.execution_backends import (
            ExecutionBackendError,
            resolve_execution,
        )

        try:
            resolved = resolve_execution(inputs["scenario_version"], inputs["manifest"])
        except ExecutionBackendError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": error.code, "message": str(error)}},
            )
        return service.create_run(
            inputs["scenario_version"], resolved, inputs["case_ids"],
            requested_manifest=body,
        )

    @application.post("/api/v1/benchmarks/external/ceval/prepare")
    def external_ceval_prepare(body: dict):
        return _external_prepare("ceval", body)

    @application.post("/api/v1/benchmarks/external/{benchmark_id}/prepare")
    def external_benchmark_prepare(benchmark_id: str, body: dict):
        if benchmark_id not in _EXTERNAL_BENCHMARKS:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "BENCHMARK_UNKNOWN", "message": benchmark_id}},
            )
        return _external_prepare(benchmark_id, body)

    @application.get("/api/v1/benchmarks/external/catalog")
    def external_catalog_list():
        _external_catalog_sync()
        return {
            "benchmarks": [
                external_catalog.status(benchmark_id)
                for benchmark_id in _EXTERNAL_BENCHMARKS
            ],
        }

    @application.get("/api/v1/benchmarks/external/ceval/cases")
    def external_ceval_cases(offset: int = 0, limit: int = 50, query: str = ""):
        return _external_cases_view("ceval", offset, limit, query)

    @application.get("/api/v1/benchmarks/external/{benchmark_id}/cases")
    def external_benchmark_cases(
        benchmark_id: str, offset: int = 0, limit: int = 50, query: str = "",
    ):
        if benchmark_id not in _EXTERNAL_BENCHMARKS:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "BENCHMARK_UNKNOWN", "message": benchmark_id}},
            )
        return _external_cases_view(benchmark_id, offset, limit, query)

    @application.get("/api/v1/benchmarks/external/ceval/preflight")
    def external_ceval_preflight(
        model: str = "",
        scope: str = "custom-subset",
        split: str = "",
        few_shot: int = 0,
        few_shot_split: str = "",
    ):
        return _external_preflight_view("ceval", {
            "model": model, "scope": scope, "split": split or None,
            "few_shot": few_shot, "few_shot_split": few_shot_split or None,
        })

    @application.get("/api/v1/benchmarks/external/{benchmark_id}/preflight")
    def external_benchmark_preflight_route(
        benchmark_id: str,
        model: str = "",
        scope: str = "custom-subset",
        split: str = "",
        few_shot: int = 0,
        few_shot_split: str = "",
    ):
        if benchmark_id not in _EXTERNAL_BENCHMARKS:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "BENCHMARK_UNKNOWN", "message": benchmark_id}},
            )
        return _external_preflight_view(benchmark_id, {
            "model": model, "scope": scope, "split": split or None,
            "few_shot": few_shot, "few_shot_split": few_shot_split or None,
        })

    @application.post("/api/v1/benchmarks/external/ceval/runs", status_code=202)
    def external_ceval_run(body: dict):
        return _external_run_create("ceval", body)

    @application.post("/api/v1/benchmarks/external/{benchmark_id}/runs", status_code=202)
    def external_benchmark_run(benchmark_id: str, body: dict):
        if benchmark_id not in _EXTERNAL_BENCHMARKS:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "BENCHMARK_UNKNOWN", "message": benchmark_id}},
            )
        return _external_run_create(benchmark_id, body)

    @application.get("/api/v1/runs/{run_id}/external-jobs")
    def run_external_jobs(run_id: str):
        external_jobs = getattr(service.store, "external_jobs", None)
        if external_jobs is None:
            return {"jobs": []}
        jobs = external_jobs.jobs_for_run(run_id)
        for job in jobs:
            checkpoint = job.get("checkpoint") or {}
            if isinstance(checkpoint, dict):
                job["metrics"] = checkpoint.get("cursor") or {}
                job["evidence"] = checkpoint.get("evidence") or {}
        return {"jobs": jobs}


    # --------------------------------------------- Terminal-Bench（Harbor，M3-T09）

    from motte_sdk import terminalbench as tb

    def _tb_dataset_or_404(dataset_revision):
        record = tb.prepared_dataset(service.store, dataset_revision=dataset_revision)
        if record is None:
            return None, JSONResponse(
                status_code=422,
                content={"error": {
                    "code": "DATASET_UNPREPARED",
                    "message": "no Terminal-Bench task set is prepared yet",
                }},
            )
        return record, None

    def _tb_model_record(model_id, *, required=True):
        """模型档案解析：oracle 不需要模型，真实 Agent 必须有已发布模型（R10/R20）。"""
        if model_id is None or model_id == "":
            if not required:
                return None, None
            return None, JSONResponse(
                status_code=422,
                content={"error": {"code": "MODEL_REQUIRED",
                                   "message": "model profile id is required"}},
            )
        record = resources.models.get(model_id)
        if record is None:
            return None, JSONResponse(
                status_code=422,
                content={"error": {"code": "MODEL_NOT_FOUND",
                                   "message": f"unknown model profile: {model_id}"}},
            )
        # 已发布模型：lifecycle 是资源生命周期的唯一判据（与 publish 路由一致）。
        if str(record.get("lifecycle") or record.get("status") or "") != "published":
            return None, JSONResponse(
                status_code=422,
                content={"error": {
                    "code": "MODEL_NOT_PUBLISHED",
                    "message": "Terminal-Bench runs require a published model profile",
                }},
            )
        return record, None

    #: Terminal-Bench 公共请求 DTO（review R16/R20/R2-06）：字段名、类型与取值都在
    #: 这里收口；未知字段与非法类型一律 4xx，绝不能变成 500。字段校验（
    #: ``_tb_run_fields``）不访问任何 repository，必须在数据集/模型查询之前完成
    #: （review R2-12：``dataset_revision`` 直接进 SQL 参数就是 500）。
    _TB_RUN_KEYS = frozenset({
        "model", "agent_id", "agent_version", "n_trials", "task_keys",
        "dataset_revision", "aggregation", "timeouts", "resources", "credentials",
    })
    _TB_TIMEOUT_KEYS = (
        "agent_sec", "verifier_sec", "agent_setup_sec", "environment_build_sec", "job_sec",
    )
    _TB_RESOURCE_KEYS = ("cpus", "memory_mb", "storage_mb", "gpus")
    _TB_AGGREGATIONS = ("first-trial", "mean-success")

    class _TBRequestError(ValueError):
        """公共请求不合法：``code`` 直接进入 4xx 响应体。"""

        def __init__(self, code, message, details=None):
            super().__init__(message)
            self.code = code
            self.details = details or {}

    def _tb_int(body, key, *, default, minimum=1, maximum=None):
        value = body.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _TBRequestError(
                "REQUEST_FIELD_TYPE_INVALID",
                f"{key} must be an integer, got {type(value).__name__}",
            )
        if value < minimum or (maximum is not None and value > maximum):
            raise _TBRequestError(
                "REQUEST_FIELD_OUT_OF_RANGE",
                f"{key} must be between {minimum} and {maximum}, got {value}",
            )
        return value

    def _tb_str(body, key, *, default=""):
        value = body.get(key, default)
        if value is None:
            return default
        if not isinstance(value, str):
            raise _TBRequestError(
                "REQUEST_FIELD_TYPE_INVALID",
                f"{key} must be a string, got {type(value).__name__}",
            )
        return value

    def _tb_number_mapping(body, key, allowed):
        value = body.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise _TBRequestError(
                "REQUEST_FIELD_TYPE_INVALID", f"{key} must be an object",
            )
        unknown = sorted(set(value) - set(allowed))
        if unknown:
            raise _TBRequestError(
                "REQUEST_FIELD_UNKNOWN",
                f"unknown {key} field(s): {', '.join(unknown)}",
                {"allowed": list(allowed)},
            )
        checked = {}
        for name, item in value.items():
            if item is None:
                continue
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise _TBRequestError(
                    "REQUEST_FIELD_TYPE_INVALID", f"{key}.{name} must be a number",
                )
            if float(item) <= 0:
                raise _TBRequestError(
                    "REQUEST_FIELD_OUT_OF_RANGE", f"{key}.{name} must be > 0",
                )
            checked[name] = item
        return checked

    def _tb_credentials(value):
        """凭据只接受 ``{name: {"ref": "env:VAR"}}`` 引用形式（review R2-06）。

        与平台侧 ``credential_refs`` 同一语义，但作为公共入口必须给出可解释的
        4xx：明文值/``{"value": ...}`` 按秘密处理（``SECRET_VALUE_IN_CREDENTIALS``），
        其它形状按缺引用处理（``CREDENTIAL_REF_REQUIRED``）。错误信息只报名字，
        绝不回显传进来的值——拒绝路径同样不能成为泄露点。
        """
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise _TBRequestError(
                "REQUEST_FIELD_TYPE_INVALID",
                "credentials must be an object mapping names to {'ref': 'env:VAR'} references",
            )
        refs = {}
        for name, item in value.items():
            label = str(name)
            if not label.strip():
                raise _TBRequestError(
                    "REQUEST_FIELD_TYPE_INVALID", "credentials names must not be empty",
                )
            candidate = item.get("ref") if isinstance(item, dict) else None
            if (
                isinstance(item, dict)
                and set(item) == {"ref"}
                and isinstance(candidate, str)
                and candidate.startswith("env:")
                and len(candidate) > len("env:")
            ):
                # 冻结 Profile 保留平台侧唯一的嵌套引用形状：
                # ``{name: {"ref": "env:VAR"}}``（``credential_refs`` 的输入）。
                refs[label] = {"ref": candidate}
                continue
            if isinstance(item, str) or (isinstance(item, dict) and "ref" not in item):
                raise _TBRequestError(
                    "SECRET_VALUE_IN_CREDENTIALS",
                    f"{label}: pass credentials by reference ({{'ref': 'env:NAME'}}), "
                    "never as values",
                )
            raise _TBRequestError(
                "CREDENTIAL_REF_REQUIRED",
                f"{label}: credential must be exactly {{'ref': 'env:NAME'}} with a "
                "non-empty env: prefix",
            )
        return refs

    def _tb_credentials_from_query(value):
        """预检查询参数 ``credential_refs``（逗号分隔 name=env:VAR）→ 引用表。

        与创建路径共用 ``_tb_credentials``，因此"预检说可以、创建却拒绝"不会
        再次出现（review R2-06）。
        """
        text = (value or "").strip()
        if not text:
            return {}
        refs = {}
        for chunk in text.split(","):
            item = chunk.strip()
            if not item:
                continue
            name, separator, ref = item.partition("=")
            name, ref = name.strip(), ref.strip()
            if not separator or not name or not ref or "=" in ref:
                raise _TBRequestError(
                    "CREDENTIAL_REF_REQUIRED",
                    "credential_refs must be comma-separated name=env:VAR pairs",
                )
            if not ref.startswith("env:") or len(ref) == len("env:"):
                raise _TBRequestError(
                    "SECRET_VALUE_IN_CREDENTIALS",
                    f"{name}: pass credentials by reference (env:NAME), never as values",
                )
            refs[name] = {"ref": ref}
        return refs

    def _tb_run_fields(body):
        """完整 DTO 字段校验（类型/取值/凭据形式），零 repository 访问。"""
        unknown = sorted(set(body) - _TB_RUN_KEYS)
        if unknown:
            raise _TBRequestError(
                "REQUEST_FIELD_UNKNOWN",
                f"unknown request field(s): {', '.join(unknown)}",
                {"allowed": sorted(_TB_RUN_KEYS)},
            )
        aggregation = _tb_str(body, "aggregation", default="first-trial") or "first-trial"
        if aggregation not in _TB_AGGREGATIONS:
            raise _TBRequestError(
                "REQUEST_FIELD_OUT_OF_RANGE",
                f"aggregation must be one of {list(_TB_AGGREGATIONS)}",
            )
        task_keys = body.get("task_keys")
        if task_keys is None:
            task_keys = []
        if not isinstance(task_keys, list) or any(
            not isinstance(item, str) for item in task_keys
        ):
            raise _TBRequestError(
                "REQUEST_FIELD_TYPE_INVALID", "task_keys must be a list of task_key strings",
            )
        return {
            "agent_id": _tb_str(body, "agent_id", default="oracle") or "oracle",
            "agent_version": _tb_str(body, "agent_version", default="1.0.0") or "1.0.0",
            "n_trials": _tb_int(body, "n_trials", default=1, maximum=32),
            "aggregation": aggregation,
            "task_keys": [str(item) for item in task_keys],
            "timeouts": _tb_number_mapping(body, "timeouts", _TB_TIMEOUT_KEYS),
            "resources": _tb_number_mapping(body, "resources", _TB_RESOURCE_KEYS),
            "credentials": _tb_credentials(body.get("credentials")),
            "model_id": _tb_str(body, "model"),
            # dataset_revision 必须是字符串或 null，且在任何查询之前校验。
            "dataset_revision": _tb_str(body, "dataset_revision", default=""),
        }

    def _tb_profile_from_fields(fields, *, strict_agent=True):
        """已校验字段 → 冻结 Profile（到这里才访问模型 repository）。"""
        profile = tb.terminal_bench_profile(
            agent_id=fields["agent_id"],
            agent_version=fields["agent_version"],
            n_trials=fields["n_trials"],
            aggregation=fields["aggregation"],
            timeouts=fields["timeouts"],
            resources=fields["resources"],
            credentials=fields["credentials"],
        )
        model_id = fields["model_id"]
        if model_id:
            record, error = _tb_model_record(model_id)
            if error is not None:
                raise _TBRequestError(
                    "MODEL_UNAVAILABLE", f"model {model_id!r} cannot be used for this run",
                )
            profile["model"] = tb.published_model_config(record)
        # Agent 能力用**创建时同一判定**先报出精确错误码（review R20）：不支持
        # 的 Agent 不该只得到笼统的 PREFLIGHT_FAILED。预检例外：它必须返回
        # 携带 reason_codes 的报告对象，所以由 ``strict_agent=False`` 跳过，
        # 能力缺口仍会作为原因码出现在报告里。
        if strict_agent:
            from motte_benchmark.harbor.config import HarborConfigError, resolve_agent_profile

            try:
                resolve_agent_profile(profile)
            except HarborConfigError as error:
                raise _TBRequestError(error.code, str(error)) from error
        return profile

    def _tb_request_error(error):
        return JSONResponse(
            status_code=422,
            content={"error": {
                "code": getattr(error, "code", "REQUEST_INVALID"),
                "message": str(error),
                "details": getattr(error, "details", {}) or {},
            }},
        )

    def _tb_display_payload(payload):
        """公共展示边界兜底：内容字段（含 note/错误文本）脱敏，身份/hash 保留。

        门面已经脱敏一次；这里再走同一函数是为了让"内容字段"的定义只有一处，
        且上游（SDK）遗漏的内容字段不会自动成为公共泄露点（review R2-07）。
        """
        from motte_sdk.terminalbench import redact_display_text

        return redact_display_text(payload)

    def _tb_docker_probe():
        """Runner 侧探测摘要：API 进程不执行 docker；没有上报就按不可用处理。"""
        import os as _os

        report = _load_json_file(_os.environ.get("MOTTE_HARBOR_PREFLIGHT_REPORT")) or {}
        return {
            "available": bool(report.get("available")),
            "server_version": report.get("server_version"),
            "platform": report.get("platform"),
            "disk_free_bytes": report.get("disk_free_bytes"),
            "disk_required_bytes": report.get("disk_required_bytes"),
            "images": list(report.get("images") or []),
        }

    def _tb_run_or_404(run_id):
        try:
            return service.get_run(run_id)
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "RUN_NOT_FOUND", "message": run_id}},
            )

    def _tb_run_summary(run):
        rows = tb.task_rows(service.store, str(run.get("id")), manifest=run.get("manifest") or {})
        aggregate = rows[0]["aggregate"] if rows else {}
        return {
            "id": run.get("id"),
            "status": run.get("status"),
            "created_at": run.get("created_at"),
            "valid_trial_pass_rate": aggregate.get("valid_trial_pass_rate"),
            "valid_trial_coverage": aggregate.get("valid_trial_coverage"),
        }

    @application.get("/api/v1/benchmarks/terminal-bench")
    def terminal_bench_overview():
        from motte_benchmark.registry import registered_adapter_ids

        record = tb.prepared_dataset(service.store)
        runs = [
            run for run in service.store.runs.list()
            if str(run.get("scenario_version") or "").startswith("terminal-bench-harbor@")
        ]
        items = []
        if record is not None:
            items.append({
                "scenario": tb.SCENARIO_VERSION,
                "dataset_revision": record.get("dataset_revision"),
                "source_id": (record.get("manifest") or {}).get("source_id"),
                "task_root": record.get("task_root"),
                "license_id": record.get("license_id"),
                "manifest_hash": record.get("manifest_hash"),
                "tasks": len(tb.task_view(record)),
                "runner_connected": tb.ADAPTER_ID in registered_adapter_ids(),
                "runs": [_tb_run_summary(run) for run in runs],
            })
        return {
            "benchmark": tb.BENCHMARK_ID,
            "items": items,
            "total": len(items),
            "runner": {
                "adapter_id": tb.ADAPTER_ID,
                "harbor_version": tb.HARBOR_VERSION,
                "connected": tb.ADAPTER_ID in registered_adapter_ids(),
            },
        }

    @application.post("/api/v1/benchmarks/terminal-bench/prepare", status_code=201)
    def terminal_bench_prepare(body: dict):
        """只读准备受控任务根目录（不执行任务包脚本、不启动任务/模型）。"""
        rejected = _reject_secret_fields(body)
        if rejected is not None:
            return rejected
        try:
            record = tb.prepare_terminal_bench_dataset(
                service.store,
                task_root=str(body.get("task_root") or ""),
                source_id=str(body.get("source_id") or ""),
                dataset_revision=str(body.get("revision") or ""),
                license_id=body.get("license_id"),
                license_evidence=body.get("license_evidence"),
                source_kind="pinned-source" if body.get("pinned_source") else "local",
            )
        except Exception as error:  # noqa: BLE001 - 合同错误以 422 返回
            return JSONResponse(
                status_code=422,
                content={"error": {
                    "code": getattr(error, "code", "TASK_PREPARE_FAILED"),
                    "message": str(error),
                }},
            )
        manifest = record.get("manifest") or {}
        tasks = tb.task_view(record)
        return {
            "state": record.get("state"),
            "dataset_revision": record.get("dataset_revision"),
            "source_id": manifest.get("source_id"),
            "manifest_hash": record.get("manifest_hash"),
            "tasks": tasks,
            "total": len(tasks),
            "invalid_tasks": manifest.get("invalid_tasks") or [],
            "license_id": record.get("license_id"),
            "license_evidence": record.get("license_evidence"),
        }

    @application.get("/api/v1/benchmarks/terminal-bench/tasks")
    def terminal_bench_tasks(dataset_revision: str | None = None):
        record, error = _tb_dataset_or_404(dataset_revision)
        if error is not None:
            return error
        items = tb.task_view(record)
        return {
            "items": items, "total": len(items),
            "dataset_revision": record.get("dataset_revision"),
        }

    @application.get("/api/v1/benchmarks/terminal-bench/preflight")
    def terminal_bench_preflight(
        model: str = "", n_trials: int = 1, task_keys: str = "",
        agent_id: str = "oracle", agent_version: str = "1.0.0",
        dataset_revision: str | None = None, credential_refs: str = "",
    ):
        """只读预检：与创建请求使用同一组能力判定与凭据入口（review R20/R2-06）。

        引用以 ``credential_refs=provider=env:ANTHROPIC_API_KEY`` 传入，逗号分隔；
        与创建路径共用同一解析/校验函数，所以预检不可能对"引用齐全"的真实
        Agent 给出与创建相反的结论。
        """
        try:
            fields = _tb_run_fields({
                "model": model, "agent_id": agent_id, "agent_version": agent_version,
                "n_trials": n_trials, "task_keys": task_keys.split(",") if task_keys else [],
                "dataset_revision": dataset_revision,
                "credentials": _tb_credentials_from_query(credential_refs),
            })
        except _TBRequestError as request_error:
            return _tb_request_error(request_error)
        record, error = _tb_dataset_or_404(fields["dataset_revision"])
        if error is not None:
            return error
        try:
            profile = _tb_profile_from_fields(fields, strict_agent=False)
        except _TBRequestError as request_error:
            return _tb_request_error(request_error)
        _model_record, model_error = _tb_model_record(
            fields["model_id"] or None, required=False,
        )
        if model_error is not None:
            return model_error
        try:
            report = tb.preflight_terminal_bench(
                record=record, profile=profile,
                task_keys=fields["task_keys"] or None,
                docker=_tb_docker_probe(),
            )
        except Exception as error:  # noqa: BLE001 - 任务集/配置问题不是 500
            return _tb_request_error(error)
        return {
            "ok": report["allowed"],
            "reasons": report["reason_codes"],
            "messages": report["messages"],
            "checks": report["checks"],
            "profile_fingerprint": report["profile_fingerprint"],
            "platform_custom_profile": report["platform_custom_profile"],
            "model": model,
            "credential_refs": sorted(fields["credentials"]),
            "dataset_revision": report.get("dataset_revision"),
            "tasks": report["tasks"],
        }

    @application.post("/api/v1/benchmarks/terminal-bench/runs", status_code=202)
    def terminal_bench_run(body: dict):
        # credentials 子树由 ``_tb_credentials`` 严格收口（只可能是 {"ref": "env:VAR"}），
        # 因此不走通用明文键名扫描——否则 ``{"anthropic_api_key": {...}}`` 这类
        # 合法引用会被键名规则误伤；其余字段仍先过通用扫描。
        rejected = _reject_secret_fields({
            key: value for key, value in body.items() if key != "credentials"
        })
        if rejected is not None:
            return rejected
        # 1) 字段类型/取值/凭据形式：零 repository 访问（review R2-12）。
        try:
            fields = _tb_run_fields(body)
        except _TBRequestError as request_error:
            return _tb_request_error(request_error)
        record, error = _tb_dataset_or_404(fields["dataset_revision"])
        if error is not None:
            return error
        # 2) 到这里才查模型与 Agent 能力。
        try:
            profile = _tb_profile_from_fields(fields)
        except _TBRequestError as request_error:
            return _tb_request_error(request_error)
        from motte_benchmark.registry import registered_adapter_ids

        if tb.ADAPTER_ID not in registered_adapter_ids():
            return JSONResponse(
                status_code=422,
                content={"error": {
                    "code": "RUNNER_NOT_CONNECTED",
                    "message": "the Harbor adapter is not registered in this process",
                }},
            )
        selected = list(fields["task_keys"])
        try:
            report = tb.preflight_terminal_bench(
                record=record, profile=profile, task_keys=selected or None,
                docker=_tb_docker_probe(),
            )
            if not report["allowed"]:
                return JSONResponse(
                    status_code=422,
                    content={"error": {
                        "code": "PREFLIGHT_FAILED",
                        "message": "Terminal-Bench preflight did not pass",
                        "details": {
                            "reasons": report["reason_codes"],
                            "messages": report["messages"],
                            "checks": report["checks"],
                        },
                    }},
                )
            from uuid import uuid4 as _uuid4

            from motte_sdk.execution_backends import resolve_execution

            planned_run_id = f"run-{_uuid4().hex}"
            inputs = tb.build_run_inputs(
                record=record, run_id=planned_run_id, job_id=f"job-{_uuid4().hex}",
                profile=profile, task_keys=selected or None,
            )
            resolved = resolve_execution(tb.SCENARIO_VERSION, inputs["manifest"])
            run = service.create_run(
                tb.SCENARIO_VERSION, resolved, inputs["case_ids"],
                requested_manifest={"benchmark": tb.BENCHMARK_ID},
                run_id=planned_run_id,
            )
        except Exception as error:  # noqa: BLE001 - 配置/任务集问题一律 4xx
            return _tb_request_error(error)
        return {
            "id": run["id"], "status": run["status"], "scenario": tb.SCENARIO_VERSION,
            "execution": resolved.get("execution") or {},
            "trials": len(inputs["trials"]),
        }

    @application.get("/api/v1/runs/{run_id}/tasks")
    def run_terminal_bench_tasks(run_id: str):
        run = _tb_run_or_404(run_id)
        if isinstance(run, JSONResponse):
            return run
        items = tb.task_rows(service.store, run_id, manifest=run.get("manifest") or {})
        return {"run_id": run_id, "items": items, "total": len(items), "status": run.get("status")}

    @application.get("/api/v1/runs/{run_id}/tasks/{task_key}/trials")
    def run_terminal_bench_task_trials(run_id: str, task_key: str):
        run = _tb_run_or_404(run_id)
        if isinstance(run, JSONResponse):
            return run
        items = tb.trial_rows(service.store, run_id, task_key)
        return {"run_id": run_id, "task_key": task_key, "items": items, "total": len(items)}

    @application.get("/api/v1/runs/{run_id}/trials/{trial_id}")
    def run_terminal_bench_trial(run_id: str, trial_id: str):
        run = _tb_run_or_404(run_id)
        if isinstance(run, JSONResponse):
            return run
        # 展示边界：内容字段脱敏、身份/hash 保留（review R08/R2-07）。
        detail = tb.trial_detail(service.store, run_id, trial_id)
        if detail is None:
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "code": "TRIAL_NOT_FOUND",
                    "message": f"trial {trial_id} does not belong to run {run_id}",
                }},
            )
        return _tb_display_payload(detail)

    @application.get("/api/v1/runs/{run_id}/trials/{trial_id}/artifacts/{artifact_id:path}/bytes")
    def run_terminal_bench_trial_artifact_bytes(
        run_id: str, trial_id: str, artifact_id: str,
    ):
        """导出冻结证据字节：默认与普通内容读取遵守**同一秘密保护边界**。

        review R2-07：归属与 hash 校验不等于展示保护，所以

        - 文本工件（UTF-8 可解码）：返回**脱敏后**的字节；冻结证据的原始 hash
          用 ``X-Motte-Artifact-Source-Sha256`` 标注（身份不被改写），响应头
          如实标注本次已脱敏；
        - 无法扫描的二进制：默认拒绝（``ARTIFACT_RAW_EXPORT_DISABLED``），只有
          操作员显式设置 ``MOTTE_ALLOW_RAW_ARTIFACT_EXPORT=1`` 才导出原始字节，
          此时响应头标注未脱敏。
        """
        import os as _os

        from fastapi import Response

        run = _tb_run_or_404(run_id)
        if isinstance(run, JSONResponse):
            return run
        ref = tb.trial_artifact_ref(service.store, run_id, trial_id, artifact_id)
        if ref is None:
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "code": "ARTIFACT_NOT_FOUND",
                    "message": (
                        f"artifact {artifact_id} does not belong to trial {trial_id} "
                        f"of run {run_id}"
                    ),
                }},
            )
        reader = tb.evidence_reader(service.store, run_id)
        try:
            data = reader.read_bytes(artifact_id)
        except (FileNotFoundError, ValueError, OSError) as error:
            return JSONResponse(
                status_code=422,
                content={"error": {
                    "code": "ARTIFACT_CONTENT_UNAVAILABLE",
                    "message": str(error),
                }},
            )
        expected = str(ref.get("sha256") or "").removeprefix("sha256:")
        if expected:
            if hashlib.sha256(data).hexdigest() != expected:
                return JSONResponse(
                    status_code=422,
                    content={"error": {
                        "code": "ARTIFACT_HASH_MISMATCH",
                        "message": "frozen artifact bytes do not match the recorded hash",
                    }},
                )
        raw_export = str(_os.environ.get("MOTTE_ALLOW_RAW_ARTIFACT_EXPORT", "")).strip().lower()
        allow_raw = raw_export in ("1", "true", "yes", "on")
        redacted = False
        if not allow_raw:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return JSONResponse(
                    status_code=422,
                    content={"error": {
                        "code": "ARTIFACT_RAW_EXPORT_DISABLED",
                        "message": (
                            "this artifact is not UTF-8 text, so it cannot be scanned for "
                            "secrets; raw export is disabled. An operator can enable "
                            "controlled export with MOTTE_ALLOW_RAW_ARTIFACT_EXPORT=1, or "
                            "read the artifact metadata via the non-/bytes content route"
                        ),
                    }},
                )
            from motte_trace.redaction import redact_secrets

            payload = redact_secrets(text).encode("utf-8")
            redacted = True
        else:
            payload = data
        return Response(
            content=payload,
            media_type=str(ref.get("media_type") or "application/octet-stream"),
            headers={
                # 冻结证据身份逐字保留；本次响应体另附自己的 hash 以便核对。
                "X-Motte-Artifact-Source-Sha256": str(ref.get("sha256") or ""),
                "X-Motte-Artifact-Response-Sha256": hashlib.sha256(payload).hexdigest(),
                "X-Motte-Artifact-Redacted": "true" if redacted else "false",
                "Content-Disposition": "attachment",
            },
        )

    @application.get("/api/v1/runs/{run_id}/trials/{trial_id}/artifacts/{artifact_id:path}")
    def run_terminal_bench_trial_artifact(run_id: str, trial_id: str, artifact_id: str):
        """按 Trial 归属读取冻结证据内容（有界 + 脱敏；见 review R19/R2-07）。"""
        run = _tb_run_or_404(run_id)
        if isinstance(run, JSONResponse):
            return run
        content = tb.trial_artifact_content(service.store, run_id, trial_id, artifact_id)
        if content is None:
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "code": "ARTIFACT_NOT_FOUND",
                    "message": (
                        f"artifact {artifact_id} does not belong to trial {trial_id} "
                        f"of run {run_id}"
                    ),
                }},
            )
        # 展示边界兜底：``note`` 等也是内容字段（review R2-07），上游错误文本
        # 不能因为"来自 SDK"就绕过脱敏；身份/hash 字段逐字保留。
        return _tb_display_payload(content)


    # ------------------------------------------- 比较/门禁（M6-Lite 公共服务，只读）

    from motte_sdk.comparisons import ComparisonError, ComparisonService

    application.state.comparisons = comparisons_service = ComparisonService(
        service.store,
        baselines=getattr(service.store, "baselines", None),
    )

    @application.get("/api/v1/comparisons")
    def compare_runs(
        baseline: str,
        candidate: str,
        factors: str = "model",
        baseline_pass: str | None = None,
        candidate_pass: str | None = None,
    ):
        try:
            result = comparisons_service.compare(
                baseline, candidate, allowed_factors=factors.split(","),
                baseline_pass_id=baseline_pass, candidate_pass_id=candidate_pass,
            )
        except KeyError as error:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "RUN_NOT_FOUND", "message": str(error)}},
            )
        except (ValueError, ComparisonError) as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "POLICY_INVALID", "message": str(error)}},
            )
        return {
            "eligible": result.eligible,
            "reasons": list(result.reasons),
            "metric_eligibility": result.metric_eligibility,
            "case_diff": result.case_diff,
        }

    @application.post("/api/v1/gates")
    def evaluate_run_gate(body: dict):
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "RUN_REQUIRED", "message": "run_id is required"}},
            )
        policy = body.get("policy") or {}
        if not isinstance(policy, dict):
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "POLICY_INVALID", "message": "policy must be an object"}},
            )
        scoring_pass_id = body.get("scoring_pass_id")
        baseline_snapshot_id = body.get("baseline_snapshot_id")
        for name, value in (
            ("scoring_pass_id", scoring_pass_id),
            ("baseline_snapshot_id", baseline_snapshot_id),
            ("baseline_run_id", body.get("baseline_run_id")),
        ):
            if value is not None and not isinstance(value, str):
                return JSONResponse(
                    status_code=422,
                    content={"error": {
                        "code": "POLICY_INVALID",
                        "message": name + " must be a string when provided",
                    }},
                )
        try:
            conclusion = comparisons_service.evaluate_gate(
                run_id,
                policy=policy,
                baseline_run_id=body.get("baseline_run_id"),
                scoring_pass_id=(
                    scoring_pass_id if isinstance(scoring_pass_id, str) else None
                ),
                baseline_snapshot_id=(
                    baseline_snapshot_id if isinstance(baseline_snapshot_id, str) else None
                ),
            )
        except KeyError as error:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "RUN_NOT_FOUND", "message": str(error)}},
            )
        except ComparisonError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": error.code, "message": str(error)}},
            )
        # 覆盖与指标值结构化返回（review R18/R21 的消费侧）：调用方不再从
        # 规则文本里解析数值，也不再自己猜覆盖单位。
        try:
            conclusion["coverage_summary"] = comparisons_service.candidate_summary(
                run_id,
                scoring_pass_id=(
                    scoring_pass_id if isinstance(scoring_pass_id, str) else None
                ),
            )
        except ComparisonError:
            conclusion["coverage_summary"] = None
        return conclusion

    # ------------------------------------------- Direct LLM 评测（通用直连 + 数据集管理）

    from motte_contracts.dataset_sources import SourceSpec

    @application.get(
        "/api/v1/benchmarks/direct-llm/sources", response_model=DatasetSourceListResponse
    )
    def direct_llm_sources():
        from motte_sdk.dataset_sources import list_sources

        items = []
        for source in list_sources():
            items.append(
                {
                    "id": source.id,
                    "label": source.label,
                    "tier": source.tier,
                    "status": source.governance.status,
                    "distribution_scope": source.governance.distribution_scope,
                    "stable_eligible": source.governance.stable_eligible,
                    "revision": source.upstream.revision.value,
                    "license_ids": source.license.data.declared_ids,
                    "profiles": source.conversion.profiles,
                    "official_comparability": source.official_comparability.status,
                    "blocker_count": len(source.blockers),
                }
            )
        return {"items": items, "total": len(items)}

    @application.get("/api/v1/benchmarks/direct-llm/sources/{source_id}", response_model=SourceSpec)
    def direct_llm_source_detail(source_id: str):
        from motte_sdk.dataset_sources import SourcePipelineError, inspect_source

        try:
            return inspect_source(source_id)
        except SourcePipelineError as error:
            raise HTTPException(status_code=404, detail=error.message) from error

    @application.get("/api/v1/benchmarks/direct-llm", response_model=DirectLlmOverviewResponse)
    def direct_llm_overview():
        from motte_contracts.direct_llm import EVAL_KEY, SUITE
        from motte_contracts.suites import (
            validated_dataset_identity,
            validated_scenario_identity,
        )

        presets = []
        scenarios = []
        for candidate in resources.scenarios.list():
            try:
                identity = validated_scenario_identity(candidate)
            except ValueError:
                continue
            if identity is not None and identity[0] == SUITE:
                scenarios.append((candidate, identity))
        for scenario, scenario_identity in sorted(
            scenarios, key=lambda item: str(item[0].get("name"))
        ):
            dataset_name, _, dataset_version = str(scenario.get("dataset", "")).rpartition("@")
            dataset = resources.datasets.get(dataset_name, dataset_version)
            if dataset is None:
                continue
            try:
                dataset_identity = validated_dataset_identity(dataset)
            except ValueError:
                continue
            if dataset_identity != scenario_identity:
                continue
            runs = [
                run
                for run in service.store.runs.list()
                if run.get("scenario_version") == f"{scenario['name']}@{scenario['version']}"
            ]
            runs.sort(key=lambda run: run.get("created_at") or "", reverse=True)
            evaluation = dataset.get(EVAL_KEY) or {}
            contract_version = int(dataset_identity[1])
            profiles = []
            if contract_version == 2:
                profiles = [
                    {
                        "name": profile["name"],
                        "count": profile["count"],
                        "strategy": profile.get("strategy"),
                        "case_ids_sha256": profile.get("case_ids_sha256"),
                    }
                    for profile in dataset.get("profiles") or []
                ]
            presets.append(
                {
                    "scenario": f"{scenario['name']}@{scenario['version']}",
                    "dataset": scenario["dataset"],
                    "suite": SUITE,
                    "contract_version": contract_version,
                    "dataset_fingerprint": dataset.get("dataset_fingerprint"),
                    "profiles": profiles,
                    "eval": evaluation,
                    "provenance": dict(dataset.get("provenance") or {}),
                    "cases": len(dataset.get("cases", ())),
                    "runs": [
                        {
                            "id": run["id"],
                            "status": run["status"],
                            "created_at": run.get("created_at"),
                            "accuracy": _direct_llm_accuracy(
                                run, service.get_run(run["id"]).get("scores", [])
                            ),
                        }
                        for run in runs[:10]
                    ],
                }
            )
        return {"items": presets, "total": len(presets)}

    @application.get("/api/v1/benchmarks/direct-llm/builtins")
    def direct_llm_builtins():
        """仓库内置样例数据集清单（含实际题数），供控制台一键导入。"""
        from motte_sdk.direct_llm import builtin_catalog

        items = builtin_catalog()
        return {"items": items, "total": len(items)}

    @application.post(
        "/api/v1/benchmarks/direct-llm/import",
        status_code=201,
        response_model=DirectLlmImportResponse,
    )
    def direct_llm_import(body: DirectLlmImportRequest):
        """导入一份 Direct LLM JSONL（内置样例或本地内容），落成不可变数据集 + 场景。

        ``content`` 与 ``builtin`` 二选一：前者是 UTF-8 JSONL 正文（Web 上传/粘贴、CLI 走 --file
        时由调用方读文件后传入），后者是内置样例 id（数据集名与评分器默认取内置注册表）。
        省略 version 时自动选版本：完整数据集身份相同则复用，否则取下一个空号。
        同 name@version 内容不同返回 409。除 version 外，显式传入的空串不会被当成默认值。
        """
        from motte_sdk.direct_llm import (
            BUILTIN_LICENSE,
            BuiltinUnavailable,
            import_builtin_dataset,
            import_direct_llm_split,
        )

        content, builtin = body.content, body.builtin
        if (content is None) == (builtin is None):
            return _invalid(
                "exactly one of content (JSONL text) or builtin (dataset id) is required"
            )
        version = (body.version or "").strip() or None
        name = (
            body.name.strip()
            if body.name is not None
            else (builtin.strip() if builtin is not None else "direct-llm-custom")
        )
        license_id = body.license.strip() if body.license is not None else BUILTIN_LICENSE
        scorer = body.scorer.strip() if body.scorer is not None else None
        source = body.source.strip() if body.source is not None else "local-jsonl"
        try:
            if builtin is not None:
                return import_builtin_dataset(
                    builtin.strip(),
                    resources=resources,
                    name=name,
                    version=version,
                    license_id=license_id,
                    scorer=scorer,
                )
            assert content is not None
            return import_direct_llm_split(
                content.encode("utf-8"),
                name=name,
                version=version,
                license_id=license_id,
                scorer=scorer,
                source=source,
                resources=resources,
            )
        except BuiltinUnavailable as error:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "BUILTIN_UNAVAILABLE", "message": str(error)}},
            )
        except ValueError as error:
            code = (
                "RESOURCE_CONFLICT"
                if isinstance(error, ResourceConflictError)
                else "CONTRACT_INVALID"
            )
            status = 409 if isinstance(error, ResourceConflictError) else 422
            return JSONResponse(
                status_code=status, content={"error": {"code": code, "message": str(error)}}
            )

    @application.get("/api/v1/benchmarks/direct-llm/cases")
    def direct_llm_cases(dataset: str, offset: int = 0, limit: int = 50, query: str = ""):
        """分页浏览某个数据集的题目（控制台「题目」页用）：题面、期望答案、生效评分器、源文件行号。"""
        from motte_contracts.direct_llm import effective_scorers

        name, _, version = str(dataset).rpartition("@")
        record = resources.datasets.get(name, version)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "DATASET_NOT_FOUND",
                        "message": f"dataset not found: {dataset}",
                    }
                },
            )
        from motte_contracts.direct_llm import SUITE

        mismatch = _suite_mismatch(record, SUITE, dataset)
        if mismatch is not None:
            return mismatch
        cases = record.get("cases") or []
        evaluation = record.get("eval") if isinstance(record.get("eval"), dict) else {}
        if evaluation.get("version") == 2:
            default_scorer = evaluation.get("scorer")

            def scorer_label(case: dict[str, Any]) -> str | None:
                metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
                spec = metadata.get("scorer") or default_scorer
                if not isinstance(spec, dict):
                    return None
                scorer_id, scorer_version = spec.get("id"), spec.get("version")
                return (
                    f"{scorer_id}@{scorer_version}"
                    if isinstance(scorer_id, str) and isinstance(scorer_version, str)
                    else None
                )

            scorers = {case["case_id"]: scorer_label(case) for case in cases}
        else:
            scorers = effective_scorers(record)
        needle = (query or "").strip().lower()
        matched = [
            case
            for case in cases
            if not needle
            or needle in str(case.get("input", "")).lower()
            or needle in str(case.get("case_id", "")).lower()
        ]
        start = max(0, offset)
        size = min(max(1, limit), 200)
        items = [
            {
                "case_id": case["case_id"],
                "input": case["input"],
                "expected": case.get("expected"),
                "scorer": scorers.get(case["case_id"]),
                "source_line": (case.get("metadata") or {}).get("source_line"),
            }
            for case in matched[start : start + size]
        ]
        return {
            "dataset": dataset,
            "total": len(matched),
            "dataset_total": len(cases),
            "offset": start,
            "limit": size,
            "query": query or "",
            "items": items,
        }

    def _prepare_direct_llm_request(body: DirectLlmRunRequest):
        from motte_contracts.direct_llm import SUITE
        from motte_contracts.selection import CASE_SELECTION_KEY

        request = body.model_dump(mode="json", exclude_none=True)
        rejected = _reject_secret_fields(request)
        if rejected is not None:
            return rejected
        scenario = body.scenario or f"{body.dataset_name}@{body.dataset_version or '1'}"
        scenario_name, _, scenario_version = scenario.rpartition("@")
        scenario_record = resources.scenarios.get(scenario_name, scenario_version)
        if scenario_record is None:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "SCENARIO_NOT_FOUND",
                        "message": f"direct-llm scenario not found: {scenario}",
                    }
                },
            )
        mismatch = _suite_mismatch(scenario_record, SUITE, scenario)
        if mismatch is not None:
            return mismatch

        manifest: dict[str, Any] = {"model": body.model}
        if body.parameters:
            manifest["parameters"] = body.parameters
        if body.reasoning_level is not None:
            manifest["reasoning_level"] = body.reasoning_level
        if body.case_selection is not None:
            manifest[CASE_SELECTION_KEY] = body.case_selection.model_dump(
                mode="json", exclude_none=True
            )
        try:
            prepared, case_ids = prepare_run(scenario, manifest, [], resources)
        except ManifestResolutionError as error:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": error.code,
                        "message": str(error),
                    }
                },
            )
        return scenario, scenario_record, manifest, prepared, case_ids

    @application.post("/api/v1/benchmarks/direct-llm/runs", status_code=202)
    def direct_llm_run(body: DirectLlmRunRequest):
        result = _prepare_direct_llm_request(body)
        if isinstance(result, JSONResponse):
            return result
        scenario, _scenario_record, manifest, prepared, case_ids = result
        return service.create_run(scenario, prepared, case_ids, requested_manifest=manifest)

    @application.post(
        "/api/v1/benchmarks/direct-llm/dry-run", response_model=DirectLlmDryRunResponse
    )
    def direct_llm_dry_run(body: DirectLlmRunRequest):
        from motte_contracts.identity import canonical_sha256

        result = _prepare_direct_llm_request(body)
        if isinstance(result, JSONResponse):
            return result
        scenario, scenario_record, _manifest, prepared, case_ids = result
        provenance = prepared.get("benchmark_provenance") or {}
        preflight = (prepared.get("budget") or {}).get("context_preflight")
        provider = prepared.get("provider") or {}
        provider_parameters = provider.get("parameters") or {}
        max_output_tokens = provider_parameters.get("max_output_tokens", 0)
        input_upper_bound = None
        total_upper_bound = None
        context_window = None
        estimation_method = None
        if isinstance(preflight, dict):
            stats = preflight.get("stats") or {}
            input_upper_bound = (stats.get("input_tokens_upper_bound") or {}).get("max")
            total_upper_bound = (stats.get("total_tokens_upper_bound") or {}).get("max")
            context_window = preflight.get("context_window")
            estimation_method = preflight.get("method")
            max_output_tokens = preflight.get("output_tokens_reserved", max_output_tokens)
        estimated_cost = None
        price_table_version = None
        currency = None
        price_table = provider.get("price_table")
        if isinstance(price_table, dict):
            version = price_table.get("version")
            price_table_version = version if isinstance(version, str) and version else None
            price_currency = price_table.get("currency")
            currency = (
                price_currency if isinstance(price_currency, str) and price_currency else None
            )
            input_rate = price_table.get("input_per_million")
            output_rate = price_table.get("output_per_million")
            numeric_rates = all(
                type(rate) in (int, float) and rate >= 0 and rate != float("inf")
                for rate in (input_rate, output_rate)
            )
            if numeric_rates and isinstance(input_upper_bound, int):
                estimated_cost = (
                    len(case_ids)
                    * (input_upper_bound * input_rate + max_output_tokens * output_rate)
                    / 1_000_000
                )
        selection = body.case_selection
        profile = (
            selection.profile if selection is not None and selection.mode == "profile" else None
        )
        evaluation = scenario_record.get("eval") or {}
        return {
            "scenario": scenario,
            "dataset": scenario_record["dataset"],
            "contract_version": evaluation.get("version", 1),
            "plugin_version": str(provenance.get("plugin_version") or "1"),
            "selected_count": len(case_ids),
            "case_ids_sha256": canonical_sha256(case_ids),
            "profile": profile,
            "max_input_tokens_upper_bound": input_upper_bound,
            "max_output_tokens": max_output_tokens,
            "max_total_tokens_upper_bound": total_upper_bound,
            "context_window": context_window,
            "estimation_method": estimation_method,
            "estimated_cost_upper_bound": estimated_cost,
            "price_table_version": price_table_version,
            "currency": currency,
            "estimated": True,
        }


    # ------------------------------------------------------------ agent-tasks

    @application.get("/api/v1/agent-tasks")
    def agent_tasks_overview():
        """Agent 文件任务数据集清单 + 最近 Run（含多指标通过概况）。"""
        from motte_contracts.agent_tasks import SUITE

        items = []
        for scenario in resources.scenarios.list():
            if scenario.get("suite") != SUITE:
                continue
            name, _, version = str(scenario.get("dataset", "")).rpartition("@")
            dataset = resources.datasets.get(name, version)
            if dataset is None:
                continue
            runs = sorted(
                (run for run in service.store.runs.list()
                 if run.get("scenario_version")
                 == f"{scenario['name']}@{scenario['version']}"),
                key=lambda run: run.get("created_at") or "", reverse=True,
            )
            items.append({
                "scenario": f"{scenario['name']}@{scenario['version']}",
                "dataset": scenario["dataset"],
                "suite": SUITE,
                "cases": len(dataset.get("cases", ())),
                "dataset_fingerprint": dataset.get("dataset_fingerprint"),
                "runs": [
                    {
                        "id": run["id"],
                        "status": run["status"],
                        "created_at": run.get("created_at"),
                        "mode": (run.get("manifest") or {}).get("agent_config", {}).get("mode"),
                    }
                    for run in runs[:10]
                ],
            })
        items.sort(key=lambda item: item["scenario"])
        return {"items": items, "total": len(items)}

    @application.post("/api/v1/agent-tasks/import", status_code=201)
    def agent_tasks_import(body: AgentTasksImportRequest):
        """导入 Agent 文件任务数据集（JSON 数组），落成不可变数据集 + 场景。"""
        import json as _json

        from motte_sdk.agent_tasks import persist_agent_tasks_dataset

        try:
            cases = _json.loads(body.content)
            record = {"name": body.name, "version": body.version or "1", "cases": cases}
            return persist_agent_tasks_dataset(record, resources, version=body.version)
        except ValueError as error:
            code = (
                "RESOURCE_CONFLICT"
                if isinstance(error, ResourceConflictError)
                else "CONTRACT_INVALID"
            )
            status = 409 if isinstance(error, ResourceConflictError) else 422
            return JSONResponse(
                status_code=status, content={"error": {"code": code, "message": str(error)}}
            )

    @application.get("/api/v1/agent-tasks/cases")
    def agent_tasks_cases(dataset: str, offset: int = 0, limit: int = 50, query: str = ""):
        """分页浏览任务题面（操作员可见 expected/forbidden；模型侧从不投影）。"""
        from motte_contracts.agent_tasks import SUITE

        name, _, version = str(dataset).rpartition("@")
        record = resources.datasets.get(name, version)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "code": "DATASET_NOT_FOUND", "message": f"dataset not found: {dataset}",
                }},
            )
        mismatch = _suite_mismatch(record, SUITE, dataset)
        if mismatch is not None:
            return mismatch
        cases = record.get("cases") or []
        needle = (query or "").strip().lower()
        matched = [
            case for case in cases
            if not needle
            or needle in str(case.get("input", "")).lower()
            or needle in str(case.get("case_id", "")).lower()
        ]
        start, size = max(0, offset), min(max(1, limit), 200)
        items = [
            {
                "case_id": case["case_id"],
                "input": case["input"],
                "fixture": sorted((case.get("fixture") or {}).keys()),
                "expected": case.get("expected"),
                "forbidden_paths": case.get("forbidden_paths") or [],
                "limits": case.get("limits") or {},
            }
            for case in matched[start : start + size]
        ]
        return {
            "dataset": dataset, "total": len(matched), "dataset_total": len(cases),
            "offset": start, "limit": size, "query": query or "", "items": items,
        }

    def _prepare_agent_tasks_request(body):
        from motte_contracts.agent_tasks import SUITE
        from motte_contracts.selection import CASE_SELECTION_KEY

        request = body.model_dump(mode="json", exclude_none=True)
        rejected = _reject_secret_fields(request)
        if rejected is not None:
            return rejected
        scenario_name, _, scenario_version = body.scenario.rpartition("@")
        scenario_record = resources.scenarios.get(scenario_name, scenario_version or "1")
        if scenario_record is None:
            return JSONResponse(
                status_code=422,
                content={"error": {
                    "code": "SCENARIO_NOT_FOUND",
                    "message": f"agent-tasks scenario not found: {body.scenario}",
                }},
            )
        mismatch = _suite_mismatch(scenario_record, SUITE, body.scenario)
        if mismatch is not None:
            return mismatch
        manifest: dict[str, Any] = {
            "model": body.model,
            "agent": {"mode": body.mode},
        }
        if body.budget is not None:
            manifest["agent"]["budget"] = body.budget.model_dump(exclude_none=True)
        if body.case_selection is not None:
            manifest[CASE_SELECTION_KEY] = body.case_selection.model_dump(
                mode="json", exclude_none=True
            )
        try:
            prepared, case_ids = prepare_run(body.scenario, manifest, [], resources)
        except ManifestResolutionError as error:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": error.code, "message": str(error)}},
            )
        return body.scenario, manifest, prepared, case_ids

    @application.post("/api/v1/agent-tasks/runs", status_code=202)
    def agent_tasks_run(body: AgentTasksRunRequest):
        """创建 Agent Run：native-tool 不支持 tools 的模型在创建期 422（零调用）。"""
        result = _prepare_agent_tasks_request(body)
        if isinstance(result, JSONResponse):
            return result
        scenario, manifest, prepared, case_ids = result
        return service.create_run(
            scenario, prepared, case_ids, requested_manifest=manifest
        )

    @application.post("/api/v1/agent-tasks/runs/dry-run", response_model=AgentTasksDryRunResponse)
    def agent_tasks_dry_run(body: AgentTasksRunRequest):
        """预检：模式、模型能力、预算与选择，全部通过才返回摘要。"""
        from motte_contracts.identity import canonical_sha256

        result = _prepare_agent_tasks_request(body)
        if isinstance(result, JSONResponse):
            return result
        scenario, _manifest, prepared, case_ids = result
        config = prepared.get("agent_config") or {}
        return AgentTasksDryRunResponse(
            scenario=scenario,
            dataset=(prepared.get("benchmark_snapshot") or {}).get("dataset", {}).get("ref", ""),
            mode=config.get("mode", ""),
            prompt_version=config.get("prompt_version", ""),
            selected_cases=len(case_ids),
            backend=prepared["execution"]["backend_id"],
            budget=config.get("budget") or {},
        )

    def _agent_case_row(run_id: str, case_id: str) -> JSONResponse | dict[str, Any]:
        """稳定结构的 case 详情（#17：无结果也返回全部数组字段）。"""
        from motte_trace.redaction import redact_secrets

        run = service.get_run(run_id)
        if case_id not in (run.get("case_ids") or []):
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "code": "CASE_NOT_IN_RUN", "message": f"{case_id} not in run {run_id}",
                }},
            )
        row = service.store.case_runs.get(run_id, case_id)
        if row is None:
            return {
                "run_id": run_id, "case_id": case_id, "result": None, "outcome": None,
                "agent": None, "events": [], "capture_errors": [], "cleanup": None,
                "observation": None, "artifacts": [], "pending": True,
            }
        result = row.get("result") or {}
        observation = result.get("observation") or {}
        agent = result.get("agent")
        if isinstance(agent, dict):
            # #5：公共展示统一脱敏 final_output；冻结评分输入不受影响
            agent = {**agent, "final_output": redact_secrets(agent.get("final_output"))}
        return {
            "run_id": run_id, "case_id": case_id,
            "outcome": row.get("outcome"),
            "agent": agent,
            "events": redact_secrets(result.get("events") or []),
            "capture_errors": result.get("capture_errors") or [],
            "cleanup": result.get("cleanup"),
            "observation": redact_secrets(observation) if observation else observation,
            "artifacts": (observation.get("artifact_refs") or []) if observation else [],
            "pending": False,
        }

    @application.get("/api/v1/runs/{run_id}/cases/{case_id}/agent")
    def agent_case_detail(run_id: str, case_id: str):
        """样本下钻：终止原因、逐步事件、Observation 概要与产物清单。"""
        result = _agent_case_row(run_id, case_id)
        if isinstance(result, JSONResponse):
            return result
        return result

    @application.get("/api/v1/runs/{run_id}/cases/{case_id}/artifacts/content")
    def agent_artifact_content(run_id: str, case_id: str, path: str):
        """读取冻结产物内容；归属校验（run 内 case、Observation 引用清单）。

        R3 #7：身份校验（path -> artifact_id -> sha256）使用原始冻结 Observation
        引用，只对返回的展示内容做值形状脱敏——文件名被脱敏规则改写不影响读取。
        """
        from motte_trace.redaction import redact_secrets

        run = service.get_run(run_id)
        if case_id not in (run.get("case_ids") or []):
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "code": "CASE_NOT_IN_RUN",
                    "message": f"{case_id} not in run {run_id}",
                }},
            )
        row = service.store.case_runs.get(run_id, case_id)
        observation = ((row or {}).get("result") or {}).get("observation") or {}
        entry = next(
            (item for item in observation.get("artifact_refs") or [] if item.get("path") == path),
            None,
        )
        if entry is None or not entry.get("available"):
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "code": "ARTIFACT_NOT_AVAILABLE",
                    "message": f"artifact not captured for case {case_id}: {path}",
                }},
            )
        import os as _os
        from pathlib import Path as _Path

        root = _Path(_os.environ.get("ARTIFACT_ROOT", "var/artifacts"))
        target = (root / entry["artifact_id"]).resolve()
        if root.resolve() not in target.parents:
            return JSONResponse(status_code=404, content={"error": {
                "code": "ARTIFACT_NOT_AVAILABLE", "message": "artifact path invalid",
            }})
        if not target.is_file():
            return JSONResponse(status_code=404, content={"error": {
                "code": "ARTIFACT_NOT_AVAILABLE", "message": "artifact bytes missing",
            }})
        import hashlib as _hashlib

        data = target.read_bytes()
        return {
            "run_id": run_id, "case_id": case_id, "path": path,
            "media_type": entry.get("media_type"),
            "size_bytes": entry.get("size_bytes"),
            "sha256": entry.get("sha256"),
            "sha256_matches": _hashlib.sha256(data).hexdigest() == entry.get("sha256"),
            # 展示视图做值形状脱敏；评分读取的冻结原文不受影响
            "content": redact_secrets(data.decode("utf-8", errors="replace")),
        }

    @application.get("/api/v1/runs/{run_id}/invocations")
    def run_invocations(run_id: str, case_id: str | None = None):
        """持久调用日志（prepared/dispatching/settled）下钻。

        R3 #4：调用摘要含工具参数/结果片段，公共响应统一按键名 + 值形状脱敏；
        持久记录本身不动。
        """
        from motte_trace.redaction import redact_secrets

        invocations_repo = getattr(service.store, "invocations", None)
        if invocations_repo is None:
            return {"items": [], "total": 0}
        if case_id is not None:
            run = service.get_run(run_id)
            if case_id not in (run.get("case_ids") or []):
                return JSONResponse(
                    status_code=404,
                    content={"error": {
                        "code": "CASE_NOT_IN_RUN",
                        "message": f"{case_id} not in run {run_id}",
                    }},
                )
            items = invocations_repo.list_for_case(run_id, case_id)
        else:
            items = invocations_repo.list_for_run(run_id)
        return {"items": [redact_secrets(item) for item in items], "total": len(items)}

    return application



def _gsm8k_accuracy(
    run: dict[str, Any], scores: list[dict[str, Any]] | None = None
) -> float | None:
    """分母取运行自己选中的题数（子集运行按子集算，旧运行回落到数据集级 selected_count）。"""
    from motte_contracts.gsm8k import run_selected_count

    rows = scores if scores is not None else (run.get("scores") or [])
    selected = run_selected_count((run.get("manifest") or {}).get("benchmark_provenance"))
    if selected is None or len(rows) != selected:
        return None
    correct = sum(1 for score in rows if score.get("outcome") == "correct")
    return round(correct / selected, 4)


def _direct_llm_accuracy(
    run: dict[str, Any], scores: list[dict[str, Any]] | None = None
) -> float | None:
    """复用评分插件的 judged 口径；未跑完或全无判定时不给概览结论。"""
    from motte_contracts.selection import run_selected_count
    from motte_eval.direct_llm import aggregate_answers

    rows = scores if scores is not None else (run.get("scores") or [])
    selected = run_selected_count((run.get("manifest") or {}).get("benchmark_provenance"))
    if selected is None or len(rows) != selected:
        return None
    provenance = (run.get("manifest") or {}).get("benchmark_provenance") or {}
    if provenance.get("plugin_version") == "2":
        from motte_sdk.direct_llm_v2 import aggregate_direct_llm_v2

        accuracy = aggregate_direct_llm_v2(run, rows)["accuracy"]
    else:
        accuracy = aggregate_answers(rows, selected)["accuracy"]
    return round(accuracy, 4) if accuracy is not None else None


def _run_model_label(run: dict[str, Any]) -> str | None:
    """列表项模型摘要：模型档案引用 > 展开快照 > inline provider 字段。"""
    manifest = run.get("manifest") or {}
    if isinstance(manifest.get("model"), str):
        return manifest["model"]
    provider = manifest.get("provider")
    if isinstance(provider, dict) and isinstance(provider.get("model"), str):
        return provider["model"]
    return None


def _redact_agent_run_view(run: dict[str, Any]) -> dict[str, Any]:
    """agent 运行的公共 Run 详情：case 结果中的 final_output 统一脱敏（#5）。

    只影响展示视图；持久化证据与冻结评分输入不变。非 agent 运行原样返回。
    """
    manifest = run.get("manifest") or {}
    backend = (manifest.get("execution") or {}).get("backend_id")
    if backend != "builtin-agent":
        return run
    from copy import deepcopy as _deepcopy

    from motte_trace.redaction import redact_secrets

    view = _deepcopy(run)
    for case in view.get("cases") or []:
        result = case.get("result")
        if not isinstance(result, dict):
            continue
        agent = result.get("agent")
        if isinstance(agent, dict):
            agent["final_output"] = redact_secrets(agent.get("final_output"))
        observation = result.get("observation")
        if isinstance(observation, dict):
            observation["final_output"] = redact_secrets(observation.get("final_output"))
        result["events"] = redact_secrets(result.get("events") or [])
    return view


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
    versions = sorted(
        {cost["price_table_version"] for cost in costs if cost.get("price_table_version")}
    )
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
                key: value
                for key, value in pass_summary.items()
                if key not in {"scores", "passed", "aggregate"}
            }
        report["summary"].update(
            {
                key: value
                for key, value in aggregate.items()
                if key in ReportSummary.model_fields and key != "aggregate"
            }
        )
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
            if benchmark.get("plugin_version") == "2":
                aggregate_judged = aggregate.get("judged")
                if isinstance(selected, int) and isinstance(aggregate_judged, int):
                    report["summary"]["failed"] = aggregate_judged - correct
                    report["summary"]["unjudged"] = selected - aggregate_judged
            elif isinstance(selected, int):
                report["summary"]["failed"] = selected - correct
        if isinstance(accuracy, (int, float)):
            report["summary"]["pass_rate"] = accuracy
        report["cost"].update(
            known_cases=len(known_costs),
            unknown_cases=(attempted - len(known_costs)) if isinstance(attempted, int) else None,
        )
        usage = [c["result"].get("usage", {}) for c in cases if isinstance(c.get("result"), dict)]
        report["usage"] = {
            key: sum(u[key] for u in usage if key in u)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if any(key in u for u in usage)
        }
    return report


app = create_app()
