"""Compose Studio: cloud metadata and local scraping, with no Whisper dependency."""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .catalog import CollectionEdit, ImportBatch, ResourceEdit, collection_url, video_url
from .cleanup import CleanupRequest
from .local_client import CloudClient, CloudError
from .local_store import LocalStore, summary
from .local_worker import Worker
from .models import StudioSettings


class Links(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10000)


class Collection(BaseModel):
    url: str
    name: str = Field(default="", max_length=120)
    dry_run: bool = False


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["metadata", "download", "parse", "export", "pipeline"]
    resource_ids: list[str] = Field(min_length=1, max_length=10000)
    dry_run: bool = False
    force: bool = False


class RecoveryRequest(BaseModel):
    task_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,200}$")
    checked_provider: bool = False


def remote_job(job):
    state = {
        "submitting": "running",
        "exporting": "running",
        "needs_attention": "failed",
        "dismissed": "cancelled",
    }.get(job["state"], job["state"])
    created = datetime.fromtimestamp(job["created"], timezone.utc).isoformat()
    return {
        "id": "cloud-" + job["id"],
        "kind": "cloud",
        "cloud": True,
        "remote_state": job["state"],
        "state": state,
        "created": created,
        "config": {"parser": job["options"], "output": job.get("output", {})},
        "dry_run": False,
        "events": [],
        "items": [
            {
                "id": "cloud-" + job["id"],
                "title": job["title"],
                "state": state,
                "progress": 100 if state == "completed" else 0,
                "stage": "parse",
                "message": job.get("error") or "云端任务持续运行，无需保持本机在线。",
                "result": {"task_id": job.get("task_id"), "outputs": job.get("outputs", {})},
            }
        ],
    }


def create_app(data_dir=None, cloud=None, start_worker=True):
    cloud = cloud or CloudClient()
    store = LocalStore(data_dir or os.getenv("TC_LOCAL_DATA_DIR", "data/cloud-studio"))
    worker = Worker(store, cloud)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.close()

    app = FastAPI(title="云端转录工作台", lifespan=lifespan)
    app.state.store, app.state.worker = store, worker

    @app.middleware("http")
    async def protection(request: Request, call_next):
        # Local service is powerful: reject cross-site writes AND DNS-rebinding hosts.
        hostname = urlparse("http://" + request.headers.get("host", "")).hostname
        if hostname not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"detail": "只允许本机访问。"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            if urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "不允许跨站修改请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "img-src 'self' data:; media-src 'self' https:; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(CloudError)
    async def cloud_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/health")
    def health():
        return {"worker": worker.thread.is_alive()}

    @app.get("/api/library")
    def library():
        return cloud.request("GET", "/studio/library")

    @app.get("/api/settings")
    def settings():
        return cloud.request("GET", "/studio/settings")

    @app.put("/api/settings")
    def save_settings(body: StudioSettings):
        return cloud.request("PUT", "/studio/settings", body.model_dump())

    @app.post("/api/import/links")
    def links(body: Links):
        urls = list(dict.fromkeys(video_url(u) for u in body.urls))
        result = {"added": 0, "existing": 0}
        ids = []
        for offset in range(0, len(urls), 100):
            batch = ImportBatch(entries=[{"url": u} for u in urls[offset : offset + 100]])
            saved = cloud.request("POST", "/studio/import", batch.model_dump())
            result["added"] += saved["added"]
            result["existing"] += saved["existing"]
            ids.extend(saved["ids"])
        job = store.create(
            "metadata", [{"id": rid, "title": url} for rid, url in zip(ids, urls)], settings()
        )
        return {**result, "metadata_job_id": job["id"]}

    @app.post("/api/import/collection")
    def collection(body: Collection):
        url = collection_url(body.url)
        config = {**settings(), "collection": {"url": url, "name": body.name or "未命名合集"}}
        return {"id": store.create("discover", [{}], config, body.dry_run)["id"]}

    @app.patch("/api/collections/{cid}")
    def rename_collection(cid: str, body: CollectionEdit):
        return cloud.request("PATCH", f"/studio/collections/{cid}", body.model_dump())

    @app.delete("/api/collections/{cid}")
    def delete_collection(cid: str):
        return cloud.request("DELETE", f"/studio/collections/{cid}")

    @app.patch("/api/resources/{rid}")
    def edit_resource(rid: str, body: ResourceEdit):
        return cloud.request(
            "PATCH", f"/studio/resources/{rid}", body.model_dump(exclude_none=True)
        )

    @app.delete("/api/resources/{rid}")
    def delete_resource(rid: str):
        for job in store.jobs():
            if any(
                i["resource_id"] == rid and i["state"] in {"pending", "running", "waiting"}
                for i in job["items"]
            ):
                raise ValueError("请先取消该资源的本地任务。")
        return cloud.request("DELETE", f"/studio/resources/{rid}")

    @app.post("/api/cleanup")
    def delete_files(body: CleanupRequest):
        for job in store.jobs():
            if any(
                i["resource_id"] in body.resource_ids
                and i["state"] in {"pending", "running", "waiting"}
                for i in job["items"]
            ):
                raise ValueError("所选资源有本地任务，请先完成或取消任务再清理 OSS。")
        return cloud.request("POST", "/studio/cleanup", body.model_dump())

    @app.get("/api/resources/{rid}/files/{category}/{index}")
    def file(rid: str, category: str, index: int, download: bool = False, preview: bool = False):
        result = cloud.request(
            "GET",
            f"/studio/resources/{rid}/files/{category}/{index}"
            f"?download={'true' if download else 'false'}",
        )
        if preview:
            if category == "audio":
                raise ValueError("音频请使用播放器。")
            # Preview through the local service; OSS need not allow browser CORS.
            with httpx.stream("GET", result["url"], timeout=60) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        return Response("文件较大，请下载后查看。", media_type="text/plain")
                    chunks.append(chunk)
            return Response(b"".join(chunks), media_type="text/plain; charset=utf-8")
        return RedirectResponse(result["url"], status_code=307)

    @app.get("/api/jobs")
    def jobs():
        local = [summary(j) for j in store.jobs()]
        remote = [summary(remote_job(j)) for j in cloud.all("jobs")]
        return sorted(local + remote, key=lambda j: j["created"], reverse=True)

    @app.post("/api/jobs")
    def create_job(body: JobRequest):
        resources = {r["id"]: r for r in library()["resources"]}
        ids = list(dict.fromkeys(body.resource_ids))
        if any(rid not in resources for rid in ids):
            raise ValueError("所选资源已不存在，请刷新。")
        if body.kind in {"download", "metadata", "pipeline"}:
            if any(not resources[rid]["url"] for rid in ids):
                raise ValueError("此录音没有原始视频链接，请选择解析或导出。")
        return {
            "id": store.create(
                body.kind, [resources[rid] for rid in ids], settings(), body.dry_run, body.force
            )["id"]
        }

    @app.get("/api/jobs/{jid}")
    def job(jid: str):
        if jid.startswith("cloud-"):
            return remote_job(cloud.request("GET", "/studio/jobs/" + jid[6:]))
        return store.job(jid)

    @app.post("/api/jobs/{jid}/{action}")
    def control(jid: str, action: str, body: RecoveryRequest | None = None):
        if jid.startswith("cloud-"):
            if action not in {"retry", "resume", "dismiss"}:
                raise ValueError("不支持此云端操作。")
            request = (body or RecoveryRequest()).model_dump()
            if action == "dismiss" and not request["checked_provider"]:
                raise ValueError("请先检查云端提供方状态，再释放任务占用。")
            return cloud.request(
                "POST",
                f"/jobs/{jid[6:]}/{action}",
                {"checked_provider": True}
                if action == "dismiss"
                else {"task_id": request["task_id"]}
                if action == "retry"
                else None,
            )
        result = store.control(jid, action)
        if action == "cancel" and worker.active and worker.active[0] == jid:
            worker.terminate()
        return result

    @app.get("/api/items/{iid}/log")
    def log(iid: str):
        if iid.startswith("cloud-"):
            remote = cloud.request("GET", "/studio/jobs/" + iid[6:])
            return {
                "text": f"状态：{remote['state']}\n任务 ID：{remote.get('task_id') or '尚未提交'}\n"
                + remote.get("error", "")
            }
        if not any(i["id"] == iid for j in store.jobs() for i in j["items"]):
            raise HTTPException(404, "任务项不存在。")
        path = store.root / "items" / iid / "worker.log"
        if not path.exists():
            return {"text": "暂无日志。"}
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 32000))
            return {"text": stream.read().decode("utf-8", errors="replace")}

    static = Path(__file__).parent / "studio_static"

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/{asset}")
    def asset(asset: str):
        if asset not in {"app.js", "zh.js", "styles.css"}:
            raise HTTPException(404)
        return FileResponse(static / asset)

    return app


def main():
    import uvicorn

    uvicorn.run(
        create_app(), host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8787"))
    )
