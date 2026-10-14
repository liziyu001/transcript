"""SQLite is the source of truth; each operation uses its own connection."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import Settings


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "library.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT UNIQUE,
                    created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                    audio TEXT NOT NULL DEFAULT '[]', parsed TEXT NOT NULL DEFAULT '[]',
                    outputs TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS membership (
                    collection_id INTEGER REFERENCES collections(id),
                    resource_id INTEGER REFERENCES resources(id),
                    PRIMARY KEY(collection_id, resource_id));
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                    config TEXT NOT NULL, dry_run INTEGER NOT NULL, force INTEGER NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
                    resource_id INTEGER REFERENCES resources(id), state TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS items_job ON items(job_id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
                    created TEXT NOT NULL, message TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO collections VALUES (1, 'General', NULL, ?)", (now(),))
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES (1, ?)", (Settings().model_dump_json(),)
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).lastrowid

    def settings(self):
        return json.loads(self.one("SELECT value FROM settings WHERE id=1")["value"])

    def import_urls(self, entries, collection_id=1, collection=None):
        added = 0
        with self.connect() as db:
            if collection:
                db.execute(
                    "INSERT OR IGNORE INTO collections(name,url,created) VALUES (?,?,?)",
                    (collection["name"], collection["url"], now()),
                )
                collection_id = db.execute(
                    "SELECT id FROM collections WHERE url=?", (collection["url"],)
                ).fetchone()[0]
            for entry in entries:
                url = entry["url"]
                title = entry.get("title") or url.rsplit("/", 1)[-1]
                db.execute(
                    "INSERT OR IGNORE INTO resources(url,title,created) VALUES (?,?,?)",
                    (url, title, now()),
                )
                row = db.execute("SELECT id FROM resources WHERE url=?", (url,)).fetchone()
                if entry.get("title"):
                    db.execute("UPDATE resources SET title=? WHERE id=?", (title, row[0]))
                added += db.execute(
                    "INSERT OR IGNORE INTO membership VALUES (?,?)", (collection_id, row[0])
                ).rowcount
        return {"added": added, "existing": len(entries) - added, "collection_id": collection_id}

    def create_job(self, kind, ids, config, dry_run=False, force=False):
        ids = list(dict.fromkeys(ids))
        with self.connect() as db:
            for resource_id in ids:
                if (
                    resource_id is not None
                    and not db.execute(
                        "SELECT id FROM resources WHERE id=?", (resource_id,)
                    ).fetchone()
                ):
                    raise ValueError(f"Resource {resource_id} no longer exists.")
            job_id = db.execute(
                "INSERT INTO jobs(kind,state,config,dry_run,force,created,updated) VALUES (?,?,?,?,?,?,?)",
                (kind, "queued", json.dumps(config), dry_run, force, now(), now()),
            ).lastrowid
            db.executemany(
                "INSERT INTO items(job_id,resource_id,state) VALUES (?,?, 'pending')",
                [(job_id, resource_id) for resource_id in ids],
            )
        self.event(job_id, "Job queued with a snapshot of current settings.")
        return job_id

    def event(self, job_id, message):
        self.execute(
            "INSERT INTO events(job_id,created,message) VALUES (?,?,?)",
            (job_id, now(), message[:2000]),
        )

    def progress(self, item_id, stage, progress, message):
        self.execute(
            "UPDATE items SET stage=?,progress=?,message=? WHERE id=?",
            (stage, max(0, min(100, progress)), message[:2000], item_id),
        )

    def job(self, job_id):
        job = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if job:
            job["config"] = json.loads(job["config"])
            job["items"] = self.rows(
                "SELECT i.*,r.title,r.url FROM items i LEFT JOIN resources r ON r.id=i.resource_id "
                "WHERE job_id=? ORDER BY i.id",
                (job_id,),
            )
            for item in job["items"]:
                item["result"] = json.loads(item["result"])
            job["events"] = self.rows(
                "SELECT * FROM (SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT 100) ORDER BY id",
                (job_id,),
            )
        return job
