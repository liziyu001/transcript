"""One interruptible local stage. Run in a child process; checkpoint before cloud requests."""

import json
import sys
from pathlib import Path

from .catalog import ImportBatch, collection_url
from .ingest import atomic_json, upload
from .local_cache import remove_uploaded_audio
from .local_client import CloudClient, CloudError
from .local_store import LocalStore


def execute(store, job, item, cloud, download=None, uploader=None):
    root = store.root / "items" / item["id"]
    root.mkdir(parents=True, exist_ok=True)
    checkpoint = root / "checkpoint.json"
    saved = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
    kind, config = job["kind"], job["config"]

    def save(**values):
        saved.update(values)
        atomic_json(checkpoint, saved)

    if job["dry_run"]:
        if kind == "discover":
            collection_url(config["collection"]["url"])
            return {"steps": ["在本机解析合集，再将元信息写入云端；试运行不访问网络。"]}
        prerequisites = item.get("prerequisites", {})
        plans = {
            "metadata": ["在本机查询视频与分P信息，再保存至云端。"],
            "download": ["下载：本机抓取并上传 OSS。"],
            "parse": [
                "解析：使用已注册的 OSS 音频。"
                if prerequisites.get("audio")
                else "缺少前置条件：请先下载并上传音频。"
            ],
            "export": [
                "从已保存的识别结果导出至 OSS。"
                if prerequisites.get("parsed")
                else "缺少前置条件：请先完成云端解析。"
            ],
            "pipeline": ["本地下载 → 上传并注册 OSS 音频 → 云端解析 → OSS 导出。"],
        }
        return {
            "steps": plans[kind] + ["以上按排队时的云端元信息预览；试运行不抓取媒体或调用模型。"]
        }
    if kind == "discover":
        from bilibili_audio_scraper.collection import get_collection_entries

        entries = saved.get("entries")
        if entries is None:
            print("正在本机解析合集…", flush=True)
            entries = get_collection_entries(config["collection"]["url"], **config["downloader"])
            save(entries=entries, imported=0, added=0, existing=0)
        for offset in range(saved.get("imported", 0), len(entries), 100):
            batch = ImportBatch(
                entries=entries[offset : offset + 100],
                collection_url=config["collection"]["url"],
                name=config["collection"]["name"],
            )
            result = cloud.request("POST", "/studio/import", batch.model_dump())
            save(
                imported=offset + len(batch.entries),
                added=saved["added"] + result["added"],
                existing=saved["existing"] + result["existing"],
            )
            print(f"已同步 {saved['imported']} / {len(entries)} 个资源至云端", flush=True)
        return {"added": saved.get("added", 0), "existing": saved.get("existing", 0)}
    library = cloud.request("GET", "/studio/library")
    resource = next((r for r in library["resources"] if r["id"] == item["resource_id"]), None)
    if not resource:
        raise ValueError("资源已不存在。")
    rid = resource["id"]
    if kind == "metadata":
        from bilibili_audio_scraper import get_video_metadata

        metadata = get_video_metadata(resource["url"], **config["downloader"])
        cloud.request(
            "PATCH",
            f"/studio/resources/{rid}",
            {"metadata": metadata, "title": metadata.get("title") or resource["title"]},
        )
        return {"message": "视频信息已保存到云端。"}

    if kind in {"download", "pipeline"} and not saved.get("uploaded"):
        if resource["audio"] and not job["force"] and not saved.get("sources"):
            save(audio_ids=[a["id"] for a in resource["audio"]], uploaded=True)
        else:
            from bilibili_audio_scraper import AudioScraper, ScraperConfig

            print("正在本机下载音频…", flush=True)
            # Per-item directory avoids the scraper's single-part shortcut across resources.
            scraper = AudioScraper(
                ScraperConfig(
                    output_dir=root / "audio",
                    timeout=config["downloader"]["timeout"],
                    retry_attempts=config["downloader"]["retries"],
                    quiet=True,
                    overwrite=True,
                )
            )
            sources = saved.get("sources")
            if not sources:
                sources = [
                    {"path": str(s.path.resolve()), "title": s.title, "url": s.url}
                    for s in (download or scraper.download_audio)(resource["url"])
                ]
                save(sources=sources)
            bucket = None
            if uploader is None:
                import oss2

                from .storage import oss_auth

                remote = cloud.request("GET", "/config")
                bucket = oss2.Bucket(
                    oss_auth(), f"https://oss-{remote['region']}.aliyuncs.com", remote["bucket"]
                )
            audio_ids = []
            for index, source in enumerate(sources):
                print(f"正在上传 OSS：第 {index + 1} / {len(sources)} 个分P", flush=True)
                _, manifest = (uploader or upload)(
                    Path(source["path"]),
                    bucket,
                    store.root / "uploads",
                    source["title"],
                    source["url"],
                )
                print(f"正在注册云端音频：第 {index + 1} / {len(sources)} 个分P", flush=True)
                audio = cloud.request("POST", "/audio", manifest["audio"])
                audio_ids.append(audio["id"])
            print("上传完成，正在关联云端资源记录…", flush=True)
            cloud.request("PATCH", f"/studio/resources/{rid}", {"audio_ids": audio_ids})
            save(audio_ids=audio_ids, uploaded=True)
    if kind in {"download", "pipeline"} and saved.get("uploaded"):
        if remove_uploaded_audio(root):
            print("OSS 上传与资源关联已确认，本地音频已删除。", flush=True)
    if kind == "download":
        return {"message": "音频已上传 OSS。", "audio_ids": saved["audio_ids"]}
    audio_ids = (
        resource.get("audio_ids", [])
        if kind == "export"
        else saved.get("audio_ids") or [a["id"] for a in resource["audio"]]
    )
    if not audio_ids:
        raise ValueError("请先运行下载并上传，或选择完整流程。")
    jobs = cloud.all("jobs")
    remote_ids = saved.get("remote_ids", {})
    for aid in audio_ids:
        if aid in remote_ids:
            continue
        candidates = sorted(
            (j for j in jobs if j["audio_id"] == aid), key=lambda j: j["created"], reverse=True
        )
        if kind == "export":
            intent = saved.get("exports", {}).get(aid)
            if intent is None:
                prior = next(
                    (j for j in candidates if j.get("raw_key") and j["state"] == "completed"), None
                )
                if not prior:
                    raise ValueError("请先完成云端解析，再导出。")
                intent = {"job_id": prior["id"], "attempt": prior["attempt"] + 1}
            # Checkpoint intent before re-export; recovery checks the expected attempt.
            saved.setdefault("exports", {})[aid] = intent
            save(exports=saved["exports"])
            current = cloud.request("GET", f"/studio/jobs/{intent['job_id']}")
            if current["attempt"] < intent["attempt"]:
                cloud.request("POST", f"/studio/jobs/{current['id']}/export", config["output"])
            remote_ids[aid] = current["id"]
        else:
            reusable = next(
                (
                    j
                    for j in candidates
                    if j["state"] in {"queued", "submitting", "running", "exporting"}
                    or (j["state"] == "completed" and j.get("raw_key"))
                ),
                None,
            )
            if reusable and not job["force"]:
                remote_ids[aid] = reusable["id"]
            else:
                if candidates and not job["force"]:
                    raise ValueError(
                        "已有失败或待确认的云端任务，请先恢复该任务；新建付费解析需勾选重新处理。"
                    )
                # Stable across process death, HTTP response loss, cancel/resume and retry.
                request = {
                    "audio_id": aid,
                    "request_id": item["id"],
                    "options": config["parser"],
                    "output": config["output"],
                }
                remote = cloud.request("POST", "/jobs", request)
                remote_ids[aid] = remote["id"]
        save(remote_ids=remote_ids)
        print("已提交云端任务；关闭本机不会中断云端解析。", flush=True)
    return {"remote_ids": list(remote_ids.values()), "message": "等待云端解析与导出。"}


def main():
    store = LocalStore(sys.argv[1])
    job = store.job(sys.argv[2])
    item = next(i for i in job["items"] if i["id"] == sys.argv[3])
    try:
        result = execute(store, job, item, CloudClient())
        atomic_json(store.root / "items" / item["id"] / "result.json", result)
    except Exception as exc:
        # SDK exception text and downloader stderr can include credentials or signed URLs.
        print(
            f"处理失败（{type(exc).__name__}）；可重试恢复已上传的文件和已提交的任务。", flush=True
        )
        if isinstance(exc, (ValueError, CloudError)):
            print(str(exc), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
