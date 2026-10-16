"""Stage scheduler with a serial scraping lane and bounded processing concurrency."""

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .store import Store, now


class Worker:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.active = {}
        self.thread = threading.Thread(target=self.run, daemon=True, name="job-worker")

    def start(self):
        # Interrupted jobs need an explicit resume; completed items remain completed.
        with self.store.connect() as db:
            db.execute(
                "UPDATE items SET state='pending',progress=0,message='Interrupted; resume to retry' WHERE state='running'"
            )
            db.execute(
                "UPDATE jobs SET state='paused',updated=? WHERE state IN ('running','pausing')",
                (now(),),
            )
            db.execute(
                "UPDATE items SET state='cancelled' WHERE job_id IN (SELECT id FROM jobs WHERE state='cancelling') AND state='pending'"
            )
            db.execute(
                "UPDATE jobs SET state='cancelled',updated=? WHERE state='cancelling'", (now(),)
            )
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()

    def terminate(self, process):
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    # Do not abandon other running children when a scheduler operation fails.
                    for job_id in {entry["job_id"] for entry in self.active.values()}:
                        self.store.event(job_id, f"Worker error: {exc}")
                self.stop.wait(0.2)
        finally:
            for item_id, entry in list(self.active.items()):
                self.terminate(entry["process"])
                entry["log"].close()
                self.store.execute(
                    "UPDATE items SET state='pending',progress=0 WHERE id=? AND state='running'",
                    (item_id,),
                )
            self.active.clear()
            self.store.execute(
                "UPDATE jobs SET state='paused',updated=? WHERE state IN ('running','pausing')",
                (now(),),
            )

    @staticmethod
    def next_stage(job, item):
        if job["kind"] != "pipeline" or job["dry_run"]:
            return job["kind"]
        result = json.loads(item["result"])
        return next(stage for stage in ("download", "parse", "export") if stage not in result)

    @staticmethod
    def lane(job, stage):
        if job["dry_run"]:
            return "processing"
        if stage in ("download", "discover", "metadata"):
            return "scraping"
        if stage == "parse" and job["config"]["parser"]["provider"] == "whisper":
            return "local"
        return "processing"

    def tick(self):
        processing_limit = self.store.settings()["processing_concurrency"]
        # Reap all children before selecting new work. A child may have already
        # saved its next checkpoint while its OS process is still exiting.
        for item_id, entry in list(self.active.items()):
            state = self.store.one("SELECT state FROM jobs WHERE id=?", (entry["job_id"],))["state"]
            process = entry["process"]
            if state == "cancelling":
                self.terminate(process)
            if process.poll() is None:
                continue
            entry["log"].close()
            del self.active[item_id]
            if state != "cancelling":
                self.store.execute(
                    "UPDATE items SET state='failed',message=? WHERE id=? AND state='running'",
                    (f"Worker exited with code {process.returncode}. See item log.", item_id),
                )

        jobs = self.store.rows(
            "SELECT * FROM jobs WHERE state IN ('queued','running','pausing','cancelling') ORDER BY id"
        )
        for job in jobs:
            job_id = job["id"]
            busy = any(e["job_id"] == job_id for e in self.active.values())
            if job["state"] in ("pausing", "cancelling"):
                if not busy:
                    self.finish_control(job_id, job["state"])
                continue
            items = self.store.rows("SELECT * FROM items WHERE job_id=? ORDER BY id", (job_id,))
            if not busy and not any(i["state"] in ("pending", "running") for i in items):
                failures = sum(i["state"] == "failed" for i in items)
                self.store.execute(
                    "UPDATE jobs SET state=?,updated=? WHERE id=? AND state IN ('queued','running')",
                    ("failed" if failures else "completed", now(), job_id),
                )
                continue
            job["config"] = json.loads(job["config"])
            for item in items:
                if item["state"] != "pending" or item["id"] in self.active:
                    continue
                resource_id = item["resource_id"]
                if resource_id is not None and any(
                    e["resource_id"] == resource_id for e in self.active.values()
                ):
                    continue
                stage = self.next_stage(job, item)
                lane = self.lane(job, stage)
                limit = processing_limit if lane == "processing" else 1
                if sum(e["lane"] == lane for e in self.active.values()) >= limit:
                    continue
                if not self.dependencies_ready(job, item, stage):
                    continue
                self.launch(job, item, stage, lane)

    def dependencies_ready(self, job, item, stage):
        if job["dry_run"] or stage not in ("parse", "export"):
            return True
        from .stages import available

        resource = self.store.one("SELECT * FROM resources WHERE id=?", (item["resource_id"],))
        if resource is None:
            return True  # Let the stage report the missing resource.
        paths = json.loads(resource["audio" if stage == "parse" else "parsed"])
        if available([p["path"] for p in paths] if stage == "parse" else paths):
            return True
        producer = "download" if stage == "parse" else "parse"
        # Wait only when a runnable upstream job can supply the missing artifact.
        # Otherwise execute normally to report the actionable prerequisite error.
        return not self.store.one(
            "SELECT i.id FROM items i JOIN jobs j ON j.id=i.job_id "
            "WHERE i.resource_id=? AND i.id!=? AND i.state IN ('pending','running') "
            "AND j.state IN ('queued','running') AND j.dry_run=0 AND j.kind IN (?, 'pipeline') LIMIT 1",
            (item["resource_id"], item["id"], producer),
        )

    def launch(self, job, item, stage, lane):
        job_id, item_id = job["id"], item["id"]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            if state not in ("queued", "running"):
                return
            if (
                db.execute(
                    "UPDATE items SET state='running',stage=?,progress=0 WHERE id=? AND state='pending'",
                    (stage, item_id),
                ).rowcount
                != 1
            ):
                return
            db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (now(), job_id))
        log = None
        try:
            logs = self.store.root / "logs"
            logs.mkdir(exist_ok=True)
            log = (logs / f"item-{item_id}.log").open("a", encoding="utf-8")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "bilibili_transcript.web.worker",
                    str(self.store.root),
                    str(job_id),
                    str(item_id),
                    stage,
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            )
        except Exception as exc:
            if log:
                log.close()
            self.store.execute(
                "UPDATE items SET state='failed',message=? WHERE id=?", (str(exc), item_id)
            )
            return
        self.active[item_id] = {
            "process": process,
            "log": log,
            "job_id": job_id,
            "resource_id": item["resource_id"],
            "lane": lane,
        }

    def finish_control(self, job_id, state):
        final = "cancelled" if state == "cancelling" else "paused"
        with self.store.connect() as db:
            if final == "cancelled":
                db.execute(
                    "UPDATE items SET state='cancelled',message='Cancelled by user' WHERE job_id=? AND state IN ('pending','running')",
                    (job_id,),
                )
            db.execute("UPDATE jobs SET state=?,updated=? WHERE id=?", (final, now(), job_id))
        self.store.event(job_id, final.capitalize())


def run_item(root, job_id, item_id, stage=None):
    from .stages import execute

    store = Store(root)
    job = store.job(job_id)
    item = next(item for item in job["items"] if item["id"] == item_id)
    print(f"[{now()}] Job {job_id}, item {item_id}: starting {stage or job['kind']}", flush=True)
    try:
        pipeline = job["kind"] == "pipeline" and not job["dry_run"] and stage is not None
        execution_job = {**job, "kind": stage} if pipeline else job
        result = execute(store, execution_job, item)
        if pipeline:
            result = {**item["result"], **result}
        complete = not pipeline or stage == "export"
        store.execute(
            "UPDATE items SET state=?,progress=?,message=?,result=? WHERE id=?",
            (
                "completed" if complete else "pending",
                100 if complete else (33 if stage == "download" else 66),
                (
                    "Dry run complete — no resources changed"
                    if job["dry_run"]
                    else ("Complete" if complete else "Waiting for next stage")
                ),
                json.dumps(result, ensure_ascii=False),
                item_id,
            ),
        )
        print(
            f"[{now()}] {'Complete' if complete else 'Stage complete; waiting for next stage'}",
            flush=True,
        )
    except Exception as exc:
        import traceback

        traceback.print_exc()
        print(f"[{now()}] Item failed; see error above.", flush=True)
        store.execute(
            "UPDATE items SET state='failed',message=? WHERE id=?", (str(exc)[:2000], item_id)
        )
        store.event(job_id, f"Item {item_id} failed: {exc}")


if __name__ == "__main__":
    run_item(
        Path(sys.argv[1]),
        int(sys.argv[2]),
        int(sys.argv[3]),
        sys.argv[4] if len(sys.argv) > 4 else None,
    )
