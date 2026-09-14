from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from motte_sdk.service import RunService, build_run_service
from motte_sdk.replay_run import ReplayProvider


def create_app(store=None) -> FastAPI:
    service = build_run_service() if store is None else RunService(store)
    application = FastAPI(title="MoTTEavl API", version="0.1.0")
    application.state.run_service = service

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/api/v1/runs", status_code=202)
    def create_run(body: dict):
        scenario = body.get("scenario_version", "")
        if scenario.startswith("vision@"):
            return JSONResponse(status_code=422, content={"error": {"code": "MODEL_CAPABILITY_UNSUPPORTED", "message": "vision capability is unsupported"}})
        return service.create_run(scenario, body.get("manifest", {}))

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

    @application.post("/api/v1/runs/{run_id}/replay")
    def replay_run(run_id: str, body: dict):
        fixture = body.get("cases", {})
        service.provider = ReplayProvider(fixture).invoke
        return service.execute(run_id, fixture.keys())

    @application.get("/api/v1/runs/{run_id}/events")
    def events(run_id: str):
        payloads = [json.dumps(event) for event in service.events(run_id)]
        return StreamingResponse((f"data: {payload}\n\n" for payload in payloads), media_type="text/event-stream")

    return application


app = create_app()
