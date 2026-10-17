"""Serial local scraping lane with independent monitoring of durable cloud jobs."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

from .catalog import now
from .local_cache import sweep_uploaded_audio


class Worker:
    def __init__(self, store, cloud):
        self.store, self.cloud = store, cloud
        self.stop = threading.Event()
        self.process = None
        self.active = None
        self.log = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.last_poll = 0
        self.lock = None

    def start(self):
        self.lock = (self.store.root / "worker.lock").open("a+b")
        self.lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                self.lock.write(b"0")
                self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("此数据目录已有本地工作进程运行。") from None
        self.store.recover()
        sweep_uploaded_audio(self.store)
        self.thread.start()

    def terminate(self):
        process = self.process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=10)

    def close(self):
        self.stop.set()
        self.terminate()
        self.thread.join(timeout=65)
        if self.log:
            self.log.close()
        if self.lock:
            self.lock.close()

    def settle(self, job):
        if job["state"] == "cancelled":
            return
        states = {i["state"] for i in job["items"]}
        if states <= {"completed", "failed"}:
            job["state"] = "failed" if "failed" in states else "completed"
        elif job["state"] == "pausing" and "running" not in states:
            job["state"] = "paused"

    def poll_cloud(self):
        waiting = [(j, i) for j in self.store.jobs() for i in j["items"] if i["state"] == "waiting"]
        if not waiting:
            return
        jobs = {j["id"]: j for j in self.cloud.all("jobs")}
        for job, item in waiting:
            remote = [jobs.get(jid) for jid in item["result"]["remote_ids"]]

            def update(saved):
                target = next(i for i in saved["items"] if i["id"] == item["id"])
                if target["state"] != "waiting":
                    return
                if all(r and r["state"] == "completed" for r in remote):
                    target.update(state="completed", progress=100, message="云端解析与导出已完成。")
                elif any(
                    r and r["state"] in {"failed", "needs_attention", "dismissed"} for r in remote
                ):
                    target.update(
                        state="failed", message="云端任务需要处理，请在云端任务详情中恢复或重试。"
                    )
                else:
                    target.update(progress=70, message="云端处理中；本机可继续下载其他资源。")
                self.settle(saved)

            self.store.change(job["id"], update)

    def tick(self):
        if self.process:
            jid, iid = self.active
            job = self.store.job(jid)
            if job["state"] == "cancelled":
                self.terminate()
            code = self.process.poll()
            if code is not None:
                result_path = self.store.root / "items" / iid / "result.json"
                result = (
                    json.loads(result_path.read_text(encoding="utf-8"))
                    if (code == 0 and result_path.exists())
                    else {}
                )

                def update(saved):
                    if saved["state"] == "cancelled":
                        return
                    item = next(i for i in saved["items"] if i["id"] == iid)
                    state = (
                        "failed" if code else "waiting" if result.get("remote_ids") else "completed"
                    )
                    item.update(
                        state=state,
                        result=result,
                        progress=70 if state == "waiting" else 100 if state == "completed" else 0,
                        message=result.get(
                            "message",
                            "阶段已完成。" if not code else "处理失败，请查看日志后重试。",
                        ),
                    )
                    saved["events"].append({"created": now(), "message": item["message"]})
                    self.settle(saved)

                self.store.change(jid, update)
                self.log.close()
                self.process = self.log = self.active = None
        if time.monotonic() - self.last_poll > 5:
            self.last_poll = time.monotonic()
            try:
                self.poll_cloud()
            except Exception:
                # Connection failures do not fail jobs or discard cloud IDs.
                pass
        if self.process or self.stop.is_set():
            return
        jobs = sorted(self.store.jobs(), key=lambda j: j["created"])
        locked = {i["resource_id"] for j in jobs for i in j["items"] if i["state"] == "waiting"}
        for job in jobs:
            if job["state"] not in {"queued", "running"}:
                continue
            item = next(
                (
                    i
                    for i in job["items"]
                    if i["state"] == "pending" and i["resource_id"] not in locked
                ),
                None,
            )
            if not item:
                continue

            def claim(saved):
                if saved["state"] not in {"queued", "running"}:
                    return
                target = next(i for i in saved["items"] if i["id"] == item["id"])
                target.update(state="running", progress=5, message="本地工作进程正在处理…")
                saved["state"] = "running"

            claimed = self.store.change(job["id"], claim)
            if next(i for i in claimed["items"] if i["id"] == item["id"])["state"] != "running":
                continue
            folder = self.store.root / "items" / item["id"]
            folder.mkdir(parents=True, exist_ok=True)
            # An old result must never make a failed/restarted child look successful.
            (folder / "result.json").unlink(missing_ok=True)
            self.log = (folder / "worker.log").open("ab")
            self.active = job["id"], item["id"]
            try:
                self.process = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-m",
                        "transcript_cloud.local_stage",
                        str(self.store.root),
                        job["id"],
                        item["id"],
                    ],
                    stdout=self.log,
                    stderr=self.log,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
            except Exception:
                self.log.close()
                self.log = self.active = None

                def fail(saved):
                    target = next(i for i in saved["items"] if i["id"] == item["id"])
                    target.update(state="failed", message="无法启动本地工作进程。")
                    self.settle(saved)

                self.store.change(job["id"], fail)
            return

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                # Keep the worker alive after transient storage/network errors.
                pass
            self.stop.wait(0.3)
