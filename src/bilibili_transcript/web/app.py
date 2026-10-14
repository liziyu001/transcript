"""Same-origin API and static UI. Run a single application worker."""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import ImportCollection, ImportLinks, NewJob, Settings
from .store import Store, now
from .worker import Worker


def create_app(data_dir=None, start_worker=True):
    store = Store(data_dir or os.environ.get("TRANSCRIPT_DATA_DIR", "data/control-center"))
    worker = Worker(store)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.close()

    app = FastAPI(title="Transcript Studio", lifespan=lifespan)
    app.state.store = store

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        # Avoid cross-site form submissions to the local, unauthenticated service.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            if urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    {"detail": "Cross-origin writes are not allowed"}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
            )
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "worker": worker.thread.is_alive()}

    @app.get("/api/library")
    def library():
        collections = store.rows(
            "SELECT c.*,COUNT(m.resource_id) AS count FROM collections c LEFT JOIN membership m ON c.id=m.collection_id GROUP BY c.id ORDER BY c.id"
        )
        resources = store.rows("SELECT * FROM resources ORDER BY id DESC")
        membership = store.rows("SELECT * FROM membership")
        groups = {}
        for row in membership:
            groups.setdefault(row["resource_id"], []).append(row["collection_id"])
        for resource in resources:
            for field in ("audio", "parsed", "outputs"):
                resource[field] = json.loads(resource[field])
            resource["collections"] = groups.get(resource["id"], [])
        return {"collections": collections, "resources": resources}

    @app.post("/api/import/links")
    def import_links(body: ImportLinks):
        return store.import_urls([{"url": url} for url in body.urls])

    @app.post("/api/import/collection")
    def import_collection(body: ImportCollection):
        config = store.settings()
        config["collection"] = {"url": body.url, "name": body.name.strip() or "Untitled collection"}
        return {"id": store.create_job("discover", [None], config, body.dry_run)}

    @app.get("/api/settings")
    def settings():
        return store.settings()

    @app.put("/api/settings")
    def save_settings(body: Settings):
        store.execute("UPDATE settings SET value=? WHERE id=1", (body.model_dump_json(),))
        return body

    @app.get("/api/jobs")
    def jobs():
        return store.rows("""SELECT j.id,j.kind,j.state,j.created,j.updated,j.dry_run,j.force,
            COUNT(i.id) AS total,
            SUM(CASE WHEN i.state='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN i.state='failed' THEN 1 ELSE 0 END) AS failed,
            AVG(i.progress) AS progress
            FROM jobs j LEFT JOIN items i ON j.id=i.job_id GROUP BY j.id ORDER BY j.id DESC""")

    @app.post("/api/jobs")
    def create_job(body: NewJob):
        try:
            return {
                "id": store.create_job(
                    body.kind, body.resource_ids, store.settings(), body.dry_run, body.force
                )
            }
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        job = store.job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    @app.post("/api/jobs/{job_id}/{action}")
    def control_job(job_id: int, action: str):
        if action not in ("pause", "resume", "cancel", "retry"):
            raise HTTPException(404, "Unknown action")
        with store.connect() as db:
            # Serialize control requests against worker transitions.
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise HTTPException(404, "Job not found")
            state = job["state"]
            if action == "pause" and state in ("queued", "running"):
                new_state = "paused" if state == "queued" else "pausing"
            elif action == "resume" and state == "paused":
                new_state = "queued"
            elif action == "cancel" and state in ("queued", "running", "paused", "pausing"):
                new_state = "cancelling" if state in ("running", "pausing") else "cancelled"
                if new_state == "cancelled":
                    db.execute(
                        "UPDATE items SET state='cancelled' WHERE job_id=? AND state='pending'",
                        (job_id,),
                    )
            elif action == "retry" and state in ("failed", "cancelled"):
                # Retry only incomplete items, preserving the original settings snapshot.
                db.execute(
                    "UPDATE items SET state='pending',progress=0,message='',result='{}' WHERE job_id=? AND state IN ('failed','cancelled')",
                    (job_id,),
                )
                new_state = "queued"
            else:
                raise HTTPException(409, f"Cannot {action} a {state} job.")
            db.execute("UPDATE jobs SET state=?,updated=? WHERE id=?", (new_state, now(), job_id))
        store.event(job_id, f"{action.capitalize()} requested.")
        return {"state": new_state}

    @app.get("/api/items/{item_id}/log")
    def item_log(item_id: int):
        path = store.root / "logs" / f"item-{item_id}.log"
        if not path.is_file():
            return {"text": "No worker log yet."}
        with path.open("rb") as file:
            file.seek(max(0, path.stat().st_size - 32000))
            return {"text": file.read().decode("utf-8", errors="replace")}

    @app.get("/api/resources/{resource_id}/files/{category}/{index}")
    def resource_file(resource_id: int, category: str, index: int, download: bool = False):
        if category not in ("audio", "parsed", "outputs"):
            raise HTTPException(404, "Unknown file category")
        resource = store.one("SELECT * FROM resources WHERE id=?", (resource_id,))
        if not resource:
            raise HTTPException(404, "Resource not found")
        files = json.loads(resource[category])
        if index < 0 or index >= len(files):
            raise HTTPException(404, "File not found")
        path = Path(files[index]["path"] if category == "audio" else files[index]).resolve()
        if store.root not in path.parents or not path.is_file():
            raise HTTPException(404, "File missing; rerun the appropriate stage")
        return FileResponse(path, filename=path.name if download else None)

    @app.delete("/api/resources/{resource_id}")
    def delete_resource(resource_id: int):
        """Delete a resource record and optionally its files."""
        resource = store.one("SELECT * FROM resources WHERE id=?", (resource_id,))
        if not resource:
            raise HTTPException(404, "Resource not found")

        # Delete all associated files
        for category in ("audio", "parsed", "outputs"):
            files = json.loads(resource[category])
            for file_entry in files:
                path = Path(file_entry["path"] if category == "audio" else file_entry).resolve()
                if store.root in path.parents and path.is_file():
                    try:
                        path.unlink()
                    except Exception:
                        pass

        # Delete from database
        with store.connect() as db:
            db.execute("DELETE FROM membership WHERE resource_id=?", (resource_id,))
            db.execute("DELETE FROM resources WHERE id=?", (resource_id,))

        return {"success": True, "message": "Resource deleted"}

    @app.delete("/api/resources/{resource_id}/files/{category}")
    def delete_resource_files(resource_id: int, category: str):
        """Delete specific category of files (audio, parsed, or outputs) for a resource."""
        if category not in ("audio", "parsed", "outputs"):
            raise HTTPException(404, "Unknown file category")

        resource = store.one("SELECT * FROM resources WHERE id=?", (resource_id,))
        if not resource:
            raise HTTPException(404, "Resource not found")

        files = json.loads(resource[category])
        deleted_count = 0

        # Delete physical files
        for file_entry in files:
            path = Path(file_entry["path"] if category == "audio" else file_entry).resolve()
            if store.root in path.parents and path.is_file():
                try:
                    path.unlink()
                    deleted_count += 1
                except Exception:
                    pass

        # Update database
        store.execute(f"UPDATE resources SET {category}='[]' WHERE id=?", (resource_id,))

        return {"success": True, "deleted": deleted_count, "message": f"Deleted {deleted_count} {category} files"}

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
    return app


def main():
    import uvicorn

    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 8000)),
    )
