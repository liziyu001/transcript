"""Named transcript bundles: immutable OSS keys and portable ZIP paths."""

import json
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field


class BundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_ids: list[str] = Field(min_length=1, max_length=10000)
    formats: list[Literal["txt", "vtt", "srt", "json"]] = Field(
        default_factory=lambda: ["txt", "vtt", "srt", "json"], min_length=1, max_length=4
    )
    collection_id: str | None = None


def component(value, fallback="未命名"):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", str(value)).strip(" .")[:40].rstrip(" .")
    # Filenames combine two titles; stay below the 255-byte filesystem limit.
    name = name.encode("utf-8")[:96].decode("utf-8", errors="ignore").rstrip(" .")
    if not name:
        name = fallback
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    return name


def bundle_plan(catalog, body):
    from .catalog import all_records

    records = catalog.records
    audio = {a["id"]: a for a in all_records(records, "audio")}
    resources = {r["id"]: r for r in catalog.raw_resources(audio.values()) if not r.get("deleted")}
    groups = {c["id"]: c for c in all_records(records, "collection") if not c.get("deleted")}
    groups.setdefault("general", {"id": "general", "name": "通用"})
    jobs = sorted(all_records(records, "job"), key=lambda j: (j["created"], j["id"]), reverse=True)
    by_audio = {}
    for job in jobs:
        # Match the latest available export revision, as displayed in the library.
        if job.get("outputs"):
            by_audio.setdefault(job["audio_id"], job)
    files, missing, used = [], [], set()
    for rid in dict.fromkeys(body.resource_ids):
        resource = resources.get(rid)
        if not resource:
            raise ValueError("部分资源已不存在，请刷新后重试。")
        memberships = [c for c in resource.get("collections", ["general"]) if c in groups]
        if body.collection_id:
            if body.collection_id not in memberships:
                raise ValueError("所选资源不属于当前合集，请刷新后重试。")
            memberships = [body.collection_id]
        elif len(memberships) > 1:
            memberships = [c for c in memberships if c != "general"]
        memberships = memberships or ["general"]
        video_id = urlparse(resource.get("url", "")).path.rstrip("/").split("/")[-1] or rid[:12]
        video = f"{component(resource['title'])} [{component(video_id)}]"
        aids = resource.get("audio_ids", [])
        if not aids:
            missing.append(
                {
                    "resource_id": rid,
                    "video": resource["title"],
                    "source_url": resource.get("url"),
                    "reason": "尚无已注册音频或导出结果",
                }
            )
        pages = {
            str(p.get("page")): p
            for p in resource.get("metadata", {}).get("pages", [])
            if isinstance(p, dict)
        }
        for index, aid in enumerate(aids, 1):
            recording = audio.get(aid, {})
            source = recording.get("source_url") or resource.get("url", "")
            number = parse_qs(urlparse(source).query).get("p", [str(index)])[0]
            number = int(number) if number.isdigit() and 0 < int(number) < 100000 else index
            title = (
                pages.get(str(number), {}).get("part") or recording.get("title") or f"分P {number}"
            )
            job = by_audio.get(aid)
            info = {
                "resource_id": rid,
                "video": resource["title"],
                "source_url": source,
                "part": number,
                "part_title": title,
                "audio_id": aid,
            }
            for fmt in dict.fromkeys(body.formats):
                key = (job or {}).get("outputs", {}).get(fmt)
                if not key or (recording.get("cleanup") or {}).get("category") in {
                    "outputs",
                    "results",
                    "all",
                }:
                    missing.append(
                        {**info, "format": fmt, "reason": "尚未导出此格式或结果正在清理"}
                    )
                    continue
                for cid in memberships:
                    group = groups[cid]
                    folder = f"{component(group['name'])} [{component(cid[:12])}]"
                    stem = f"{component(resource['title'], '视频')}_P{number:03d}_{component(title)} [{aid[:8]}]"
                    path = f"{folder}/{video}/{stem}.{fmt}"
                    counter = 1
                    while path.casefold() in used:
                        counter += 1
                        path = f"{folder}/{video}/{stem}_{counter}.{fmt}"
                    used.add(path.casefold())
                    files.append(
                        {
                            **info,
                            "collection_id": cid,
                            "collection": group["name"],
                            "format": fmt,
                            "job_id": job["id"],
                            "key": key,
                            "path": path,
                            "url": catalog.service.artifacts.url(key, expires=3600),
                        }
                    )
    if not files:
        raise ValueError("所选资源没有可下载的导出结果，请先解析并导出所需格式。")
    return {"files": files, "missing": missing}


def build_archive(plan, directory, max_bytes=512 * 1024 * 1024):
    """Download incrementally to disk; never deliver a misleading partial ZIP."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".zip", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        total = 0
        with (
            zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive,
            httpx.Client(timeout=60) as client,
        ):
            for entry in plan["files"]:
                with client.stream("GET", entry["url"]) as response:
                    response.raise_for_status()
                    with archive.open(entry["path"], "w") as output:
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError("所选结果超过 512 MB，请分批下载。")
                            output.write(chunk)
            manifest = {
                "files": [
                    {k: v for k, v in entry.items() if k != "url"} for entry in plan["files"]
                ],
                "missing": plan["missing"],
            }
            archive.writestr("下载清单.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr(
                "说明.txt",
                "目录结构：合集 / 视频标题 [视频ID] / 视频标题_P序号_分P标题 [音频ID].格式\n同一视频属于多个合集时，会在对应合集目录中各保存一份。\n下载清单.json 包含原始链接、分P映射及未导出的格式。\n仅下载已有导出文件，不会调用语音识别。\n",
            )
        return path
    except Exception as exc:
        path.unlink(missing_ok=True)
        if isinstance(exc, httpx.HTTPError):
            raise ValueError(
                "下载 OSS 结果失败，未生成不完整的 ZIP。请刷新后重试；文件可能已被清理。"
            ) from None
        raise
