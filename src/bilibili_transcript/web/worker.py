"""One scheduler, one cancellable child process at a time, durable item checkpoints."""

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
        self.thread.join(timeout=15)

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
        while not self.stop.is_set():
            job = None
            try:
                job = self.store.one("SELECT id FROM jobs WHERE state='queued' ORDER BY id LIMIT 1")
                if job:
                    self.process_job(job["id"])
                else:
                    self.stop.wait(0.5)
            except Exception as exc:
                # Keep the scheduler alive if an individual spawn or DB operation fails.
                if job:
                    self.store.event(job["id"], f"Worker error: {exc}")
                    self.store.execute(
                        "UPDATE jobs SET state='paused',updated=? WHERE id=?", (now(), job["id"])
                    )
                    self.store.execute(
                        "UPDATE items SET state='pending' WHERE job_id=? AND state='running'",
                        (job["id"],),
                    )
                self.stop.wait(1)

    def process_job(self, job_id):
        with self.store.connect() as db:
            if (
                db.execute(
                    "UPDATE jobs SET state='running',updated=? WHERE id=? AND state='queued'",
                    (now(), job_id),
                ).rowcount
                != 1
            ):
                return
        self.store.event(job_id, "Worker started.")
        while not self.stop.is_set():
            state = self.store.one("SELECT state FROM jobs WHERE id=?", (job_id,))["state"]
            if state in ("cancelling", "pausing"):
                self.finish_control(job_id, state)
                return
            item = self.store.one(
                "SELECT id FROM items WHERE job_id=? AND state='pending' ORDER BY id LIMIT 1",
                (job_id,),
            )
            if not item:
                failures = self.store.one(
                    "SELECT COUNT(*) AS n FROM items WHERE job_id=? AND state='failed'", (job_id,)
                )["n"]
                self.store.execute(
                    "UPDATE jobs SET state=?,updated=? WHERE id=?",
                    ("failed" if failures else "completed", now(), job_id),
                )
                self.store.event(job_id, f"Finished with {failures} failed item(s).")
                return
            item_id = item["id"]
            self.store.execute("UPDATE items SET state='running',progress=0 WHERE id=?", (item_id,))
            logs = self.store.root / "logs"
            logs.mkdir(exist_ok=True)
            log_path = logs / f"item-{item_id}.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "bilibili_transcript.web.worker",
                        str(self.store.root),
                        str(job_id),
                        str(item_id),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
                try:
                    while process.poll() is None:
                        state = self.store.one("SELECT state FROM jobs WHERE id=?", (job_id,))[
                            "state"
                        ]
                        if self.stop.is_set() or state == "cancelling":
                            self.terminate(process)
                            break
                        self.stop.wait(0.3)
                finally:
                    if process.poll() is None:
                        self.terminate(process)
            if self.stop.is_set():
                self.store.execute(
                    "UPDATE items SET state='pending',progress=0 WHERE id=? AND state='running'",
                    (item_id,),
                )
                self.store.execute(
                    "UPDATE jobs SET state='paused',updated=? WHERE id=?", (now(), job_id)
                )
                return
            if state == "cancelling":
                self.finish_control(job_id, state)
                return
            remaining = self.store.one("SELECT state FROM items WHERE id=?", (item_id,))["state"]
            if remaining == "running":
                # Crash/OOM/native failures may bypass Python exception handling.
                message = f"Worker exited with code {process.returncode}. See item log."
                self.store.execute(
                    "UPDATE items SET state='failed',message=? WHERE id=?", (message, item_id)
                )
                self.store.event(job_id, message)
        self.store.execute("UPDATE jobs SET state='paused',updated=? WHERE id=?", (now(), job_id))

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


def run_item(root, job_id, item_id):
    from .stages import execute

    store = Store(root)
    job = store.job(job_id)
    item = next(item for item in job["items"] if item["id"] == item_id)
    try:
        result = execute(store, job, item)
        store.execute(
            "UPDATE items SET state='completed',progress=100,message=?,result=? WHERE id=?",
            (
                "Dry run complete — no resources changed" if job["dry_run"] else "Complete",
                json.dumps(result, ensure_ascii=False),
                item_id,
            ),
        )
    except Exception as exc:
        import traceback

        traceback.print_exc()
        store.execute(
            "UPDATE items SET state='failed',message=? WHERE id=?", (str(exc)[:2000], item_id)
        )
        store.event(job_id, f"Item {item_id} failed: {exc}")


if __name__ == "__main__":
    run_item(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
