"""Personal control panel API, hosted on FC (Lambda) or run locally for development."""

import hmac
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import AudioRegistration, Dismissal, NewJob, Recovery, Settings
from .service import Service
from .storage import Artifacts, Conflict, Flows, Records


def make_service(settings, context=None, worker=False):
    from .provider import DashScope

    return Service(
        Records(settings, context),
        Artifacts(settings, context),
        Flows(settings, context),
        DashScope(settings.dashscope_key) if worker else None,
        settings,
    )


def create_app(service=None, settings=None):
    settings = settings or Settings.from_env()
    service = service or make_service(settings)
    app = FastAPI(title="Transcript Cloud", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.allowed_origin],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    async def authenticated(request: Request):
        expected = "Bearer " + settings.panel_token
        if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(401, "Enter your personal access token")

    auth = [Depends(authenticated)]
    from .catalog import install_routes

    install_routes(app, service, auth)

    @app.get("/api/config", dependencies=auth)
    def config():
        return {
            "region": settings.region,
            "bucket": settings.bucket,
            "max_active": None,
        }

    @app.get("/api/audio", dependencies=auth)
    def audio(cursor: str | None = None):
        return service.records.list("audio", cursor)

    @app.post("/api/audio", dependencies=auth)
    def register(body: AudioRegistration):
        return service.register(body)

    @app.get("/api/jobs", dependencies=auth)
    def jobs(cursor: str | None = None):
        return service.records.list("job", cursor)

    @app.post("/api/jobs", dependencies=auth)
    def create(body: NewJob):
        return service.create(body)

    @app.post("/api/jobs/{job_id}/retry", dependencies=auth)
    def retry(job_id: str, body: Recovery):
        return service.retry(job_id, body.task_id)

    @app.post("/api/jobs/{job_id}/resume", dependencies=auth)
    def resume(job_id: str):
        return service.resume(job_id)

    @app.post("/api/jobs/{job_id}/export", dependencies=auth)
    def export(job_id: str):
        return service.reexport(job_id)

    @app.post("/api/jobs/{job_id}/dismiss", dependencies=auth)
    def dismiss(job_id: str, body: Dismissal):
        return service.dismiss(job_id)

    @app.get("/api/jobs/{job_id}/files/{fmt}", dependencies=auth)
    def download(job_id: str, fmt: str):
        job = service.require(job_id)
        key = job.get("raw_key") if fmt == "raw" else job["outputs"].get(fmt)
        if not key:
            raise HTTPException(404, "Output is not available")
        return {"url": service.artifacts.url(key, expires=900, download=True)}

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app


def main():
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8787)
