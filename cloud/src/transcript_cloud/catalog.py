"""Cloud-owned library metadata and explicit media cleanup."""

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field

from .cleanup import CleanupRequest, cleanup
from .models import StudioSettings
from .storage import Conflict


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def video_url(value):
    parsed = urlparse(value.strip())
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]+|av[0-9]+)/?", parsed.path)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"www.bilibili.com", "bilibili.com", "m.bilibili.com"}
        or parsed.username
        or parsed.port not in {None, 80, 443}
        or not match
    ):
        raise ValueError("请填写完整的哔哩哔哩视频链接。")
    return "https://www.bilibili.com/video/" + match[1]


def collection_url(value):
    parsed = urlparse(value.strip())
    match = re.fullmatch(r"/(\d+)/lists/(\d+)/?", parsed.path)
    kind = parse_qs(parsed.query).get("type", ["series"])[0]
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != "space.bilibili.com"
        or parsed.username
        or parsed.port not in {None, 80, 443}
        or not match
        or kind not in {"series", "season"}
    ):
        raise ValueError("请填写有效的哔哩哔哩合集链接（series 或 season）。")
    return f"https://space.bilibili.com/{match[1]}/lists/{match[2]}?type={kind}"


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2000)
    title: str = Field(default="", max_length=500)
    metadata: dict = Field(default_factory=dict)


class ImportBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[Entry] = Field(min_length=1, max_length=200)
    collection_url: str = ""
    name: str = Field(default="", max_length=120)


class ResourceEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=500)
    metadata: dict | None = None
    audio_ids: list[str] | None = Field(default=None, max_length=1000)
    collections: list[str] | None = Field(default=None, max_length=200)


class CollectionEdit(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def all_records(records, kind):
    result, cursor = [], None
    while True:
        page = records.list(kind, cursor)
        result.extend(page["items"])
        cursor = page.get("cursor")
        if not cursor:
            return result


class Catalog:
    def __init__(self, service):
        self.service = service
        self.records = service.records

    def change(self, kind, key, update):
        for _ in range(5):
            previous = self.records.get(kind, key)
            value = update(dict(previous or {}))
            try:
                return self.records.put(kind, key, value, (previous or {}).get("revision"))
            except Conflict:
                continue
        raise Conflict("云端记录已更改，请刷新后重试。")

    def settings(self):
        saved = self.records.get("settings", "studio") or {}
        defaults = StudioSettings(processing_concurrency=self.service.settings.max_active)
        return {
            **defaults.model_dump(),
            **{k: v for k, v in saved.items() if k in StudioSettings.model_fields},
        }

    def save_settings(self, body):
        self.change("settings", "studio", lambda old: body.model_dump())
        return self.settings()

    def import_batch(self, body):
        # Validate the complete batch before any writes. Individual writes are replayable.
        entries = [(video_url(e.url), e) for e in body.entries]
        url = collection_url(body.collection_url) if body.collection_url else ""
        cid = identity(url) if url else "general"
        self.change(
            "collection",
            cid,
            lambda old: {
                **old,
                "name": body.name or old.get("name") or "通用",
                "url": url,
                "created": old.get("created", now()),
                "deleted": False,
            },
        )
        added = existing = 0
        ids = []
        for url, entry in entries:
            rid = identity(url)
            previous = self.records.get("resource", rid)
            existing += bool(previous and not previous.get("deleted"))
            added += not previous or bool(previous.get("deleted"))

            def merge(old):
                metadata = {
                    **old.get("metadata", {}),
                    **{k: v for k, v in entry.metadata.items() if v is not None},
                }
                return {
                    **old,
                    "url": url,
                    "title": entry.title or old.get("title") or url.rsplit("/", 1)[-1],
                    "metadata": metadata,
                    "collections": sorted(set(old.get("collections", []) + [cid])),
                    "audio_ids": old.get("audio_ids", []),
                    "deleted": False,
                    "created": old.get("created", now()),
                }

            self.change("resource", rid, merge)
            ids.append(rid)
        return {"added": added, "existing": existing, "ids": ids, "collection_id": cid}

    def raw_resources(self, audio=None):
        """Include pre-Studio CLI uploads without migrating or duplicating OSS objects."""
        resources = {r["id"]: r for r in all_records(self.records, "resource")}
        linked = {a for r in resources.values() for a in r.get("audio_ids", [])}
        for recording in audio if audio is not None else all_records(self.records, "audio"):
            if recording["id"] in linked:
                continue
            try:
                url = video_url(recording.get("source_url", ""))
                rid = identity(url)
            except ValueError:
                url, rid = "", "audio-" + recording["id"]
            resource = resources.setdefault(
                rid,
                {
                    "id": rid,
                    "title": recording["title"],
                    "url": url,
                    "metadata": {},
                    "collections": ["general"],
                    "audio_ids": [],
                    "created": datetime.fromtimestamp(
                        recording["created"], timezone.utc
                    ).isoformat(),
                },
            )
            if not resource.get("deleted") and not resource.get("uploads_managed"):
                resource["audio_ids"] = list(
                    dict.fromkeys(resource["audio_ids"] + [recording["id"]])
                )
        return list(resources.values())

    def resource(self, rid):
        resource = next((r for r in self.raw_resources() if r["id"] == rid), None)
        if not resource or resource.get("deleted"):
            raise ValueError("资源不存在。")
        return resource

    def edit(self, rid, body):
        resource = self.resource(rid)
        changes = body.model_dump(exclude_none=True)
        for aid in changes.get("audio_ids", []):
            recording = self.records.get("audio", aid)
            if not recording or recording.get("state") != "ready":
                raise ValueError("音频尚未上传并注册。")
        if "audio_ids" in changes:
            changes["uploads_managed"] = True
        for cid in changes.get("collections", []):
            group = self.records.get("collection", cid)
            if cid != "general" and (not group or group.get("deleted")):
                raise ValueError("合集不存在。")
        if "metadata" in changes:
            changes["metadata_updated"] = now()
        return self.change("resource", rid, lambda old: {**resource, **old, **changes})

    def archive(self, rid):
        resource = self.resource(rid)
        return self.change("resource", rid, lambda old: {**resource, **old, "deleted": True})

    def collection_edit(self, cid, name=None):
        if cid == "general":
            raise ValueError("通用合集不能重命名或删除。")
        group = self.records.get("collection", cid)
        if not group or group.get("deleted"):
            raise ValueError("合集不存在。")
        if name is not None:
            return self.change("collection", cid, lambda old: {**old, "name": name})
        for resource in self.raw_resources():
            if cid in resource.get("collections", []):
                self.change(
                    "resource",
                    resource["id"],
                    lambda old: {
                        **resource,
                        **old,
                        "collections": [
                            c for c in old.get("collections", resource["collections"]) if c != cid
                        ]
                        or ["general"],
                    },
                )
        return self.change("collection", cid, lambda old: {**old, "deleted": True})

    def snapshot(self):
        audio = {a["id"]: a for a in all_records(self.records, "audio")}
        jobs = all_records(self.records, "job")
        resources = []
        for r in self.raw_resources(audio.values()):
            if r.get("deleted"):
                continue
            r = dict(r, audio=[], parsed=[], outputs=[])
            r["exported_parts"] = 0
            r["cleanup_pending"] = []
            r["audio_deleted_count"] = 0
            r["oss_audio_bytes"] = 0
            r["part_total"] = len(r.get("audio_ids", []))
            for aid in r.get("audio_ids", []):
                if aid not in audio:
                    continue
                a = audio[aid]
                if a.get("cleanup"):
                    r["cleanup_pending"].append(a["cleanup"]["category"])
                if a["state"] == "ready":
                    r["audio"].append(
                        {"path": a["key"], "id": aid, "title": a["title"], "size": a["size"]}
                    )
                    r["oss_audio_bytes"] += a["size"]
                elif a["state"] == "deleted":
                    r["audio_deleted_count"] += 1
                candidates = sorted(
                    (j for j in jobs if j["audio_id"] == aid),
                    key=lambda j: j["created"],
                    reverse=True,
                )
                saved = next((j for j in candidates if j.get("raw_key")), None)
                if saved:
                    r["parsed"].append(saved["raw_key"])
                exported = next((j for j in candidates if j.get("outputs")), None)
                if exported:
                    r["outputs"].extend(exported["outputs"].values())
                    r["exported_parts"] += 1
            r["parsed_complete"] = bool(r["part_total"]) and len(r["parsed"]) == r["part_total"]
            r["exported_complete"] = (
                bool(r["part_total"]) and r["exported_parts"] == r["part_total"]
            )
            resources.append(r)
        groups = {
            c["id"]: c for c in all_records(self.records, "collection") if not c.get("deleted")
        }
        groups.setdefault("general", {"id": "general", "name": "通用", "url": ""})
        for c in groups.values():
            c["count"] = sum(c["id"] in r["collections"] for r in resources)
        return {"collections": list(groups.values()), "resources": resources}


def install_routes(app, service, auth):
    from fastapi import APIRouter

    from .models import OutputOptions

    catalog = Catalog(service)
    router = APIRouter(prefix="/api/studio", dependencies=auth)

    @router.get("/library")
    def library():
        return catalog.snapshot()

    @router.get("/settings")
    def settings():
        return catalog.settings()

    @router.put("/settings")
    def save_settings(body: StudioSettings):
        return catalog.save_settings(body)

    @router.post("/import")
    def import_batch(body: ImportBatch):
        return catalog.import_batch(body)

    @router.post("/cleanup")
    def delete_files(body: CleanupRequest):
        return cleanup(catalog, body)

    @router.patch("/resources/{rid}")
    def edit(rid: str, body: ResourceEdit):
        return catalog.edit(rid, body)

    @router.delete("/resources/{rid}")
    def archive(rid: str):
        return catalog.archive(rid)

    @router.patch("/collections/{cid}")
    def collection_edit(cid: str, body: CollectionEdit):
        return catalog.collection_edit(cid, body.name)

    @router.delete("/collections/{cid}")
    def collection_delete(cid: str):
        return catalog.collection_edit(cid)

    @router.get("/resources/{rid}/files/{category}/{index}")
    def file(rid: str, category: str, index: int, download: bool = False):
        resource = next((r for r in catalog.snapshot()["resources"] if r["id"] == rid), None)
        if not resource or category not in {"audio", "parsed", "outputs"}:
            raise ValueError("文件不存在。")
        files = resource[category]
        if index < 0 or index >= len(files):
            raise ValueError("文件不存在。")
        key = files[index]["path"] if category == "audio" else files[index]
        return {"url": service.artifacts.url(key, expires=900, download=download)}

    @router.post("/jobs/{jid}/export")
    def export(jid: str, body: OutputOptions):
        return service.reexport(jid, body.model_dump())

    @router.get("/jobs/{jid}")
    def job(jid: str):
        return service.require(jid)

    app.include_router(router)
