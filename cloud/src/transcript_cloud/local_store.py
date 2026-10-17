"""Local durable queue only; the resource catalog lives in Alibaba Cloud."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .catalog import now


class LocalStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, document TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "queue.sqlite3", timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def jobs(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT document FROM jobs")]

    def job(self, jid):
        with self.connect() as db:
            row = db.execute("SELECT document FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise ValueError("任务不存在。")
        return json.loads(row[0])

    def change(self, jid, update):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise ValueError("任务不存在。")
            job = json.loads(row[0])
            update(job)
            job["updated"] = now()
            db.execute(
                "UPDATE jobs SET document=? WHERE id=?", (json.dumps(job, ensure_ascii=False), jid)
            )
        return job

    def create(self, kind, resources, config, dry_run=False, force=False):
        jid = uuid.uuid4().hex
        job = {
            "id": jid,
            "kind": kind,
            "state": "queued",
            "config": config,
            "created": now(),
            "updated": now(),
            "dry_run": dry_run,
            "force": force,
            "events": [{"created": now(), "message": "任务已加入本地队列，配置已保存。"}],
            "items": [
                {
                    "id": uuid.uuid4().hex,
                    "resource_id": r.get("id"),
                    "title": r.get("title", "合集解析"),
                    "state": "pending",
                    "stage": kind,
                    "progress": 0,
                    "message": "等待本地工作进程",
                    "result": {},
                    "prerequisites": {
                        "audio": bool(r.get("audio")),
                        "parsed": bool(r.get("parsed")),
                    },
                }
                for r in resources
            ],
        }
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES (?,?)", (jid, json.dumps(job, ensure_ascii=False)))
        return job

    def control(self, jid, action):
        def update(job):
            state = job["state"]
            allowed = {
                "pause": {"queued", "running"},
                "resume": {"paused"},
                "cancel": {"queued", "running", "paused", "pausing"},
                "retry": {"failed", "cancelled"},
            }
            if state not in allowed.get(action, set()):
                raise ValueError("当前任务状态不支持此操作。")
            if action == "pause":
                job["state"] = (
                    "pausing" if any(i["state"] == "running" for i in job["items"]) else "paused"
                )
            elif action == "cancel":
                job["state"] = "cancelled"
                for item in job["items"]:
                    if item["state"] in {"pending", "running", "waiting"}:
                        item.update(
                            state="cancelled", message="本地任务已取消；已提交的云端任务继续运行。"
                        )
            else:
                job["state"] = "queued"
                for item in job["items"]:
                    if item["state"] in {"failed", "cancelled"}:
                        item.update(state="pending", message="等待重试", progress=0)
            job["events"].append(
                {
                    "created": now(),
                    "message": {
                        "pause": "已请求暂停；云端任务继续运行。",
                        "resume": "已继续。",
                        "cancel": "已取消本地任务。",
                        "retry": "已排队重试未完成项。",
                    }[action],
                }
            )

        return self.change(jid, update)

    def recover(self):
        for job in self.jobs():
            if job["state"] in {"queued", "running", "pausing"}:

                def update(saved):
                    saved["state"] = "paused"
                    for item in saved["items"]:
                        if item["state"] == "running":
                            item.update(
                                state="pending", message="本地进程已中断；继续可恢复上传和提交。"
                            )

                self.change(job["id"], update)


def summary(job):
    items = job["items"]
    return {
        **{k: v for k, v in job.items() if k not in {"items", "events", "config"}},
        "total": len(items),
        "completed": sum(i["state"] == "completed" for i in items),
        "failed": sum(i["state"] == "failed" for i in items),
        "progress": sum(i["progress"] for i in items) / max(1, len(items)),
    }
