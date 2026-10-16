"""Stage adapters. Parsing consumes audio; exporting consumes canonical JSON."""

import json
import time
from dataclasses import asdict
from pathlib import Path

from .output import atomic_json, export_transcript


def available(paths):
    return bool(paths) and all(Path(path).is_file() and Path(path).stat().st_size for path in paths)


def plan(store, resource, kind, config, force):
    if kind == "metadata":
        return {
            "resource_id": resource["id"],
            "title": resource["title"],
            "steps": [
                {
                    "stage": "metadata",
                    "action": "Fetch title, parts and durations without downloading media",
                }
            ],
        }
    audio = json.loads(resource["audio"])
    parsed = json.loads(resource["parsed"])
    stages = ["download", "parse", "export"] if kind == "pipeline" else [kind]
    result = []
    audio_ready = available([a["path"] for a in audio])
    parsed_ready = available(parsed)
    for stage in stages:
        if stage == "download":
            reuse = audio_ready and not force
            result.append({"stage": stage, "action": "reuse audio" if reuse else "download audio"})
            if not reuse:
                parsed_ready = False
        elif stage == "parse":
            action = "reuse transcript" if parsed_ready and not force else "transcribe audio"
            if kind != "pipeline" and not audio_ready:
                action = "blocked: download audio first"
            result.append({"stage": stage, "action": action})
        else:
            action = "export " + ", ".join(config["output"]["formats"])
            if kind != "pipeline" and not parsed_ready:
                action = "blocked: parse audio first"
            result.append({"stage": stage, "action": action})
    return {"resource_id": resource["id"], "title": resource["title"], "steps": result}


def execute(store, job, item):
    config = job["config"]
    item_id = item["id"]
    job_id = job["id"]
    last_update = [0.0]

    def progress(stage, value, message, force=False):
        if force or time.monotonic() - last_update[0] > 0.5:
            store.progress(item_id, stage, value, message)
            last_update[0] = time.monotonic()

    if job["kind"] == "discover":
        if job["dry_run"]:
            return {
                "url": config["collection"]["url"],
                "steps": [
                    "Fetch collection pages (network required on actual run)",
                    "Deduplicate video URLs and add collection membership",
                    "Queue video metadata lookups without downloading media",
                ],
                "network_used": False,
            }
        from bilibili_audio_scraper.collection import get_collection_entries

        progress("discover", 0, "Fetching collection pages…", True)
        entries = get_collection_entries(
            config["collection"]["url"],
            config["downloader"]["timeout"],
            config["downloader"]["retries"],
            lambda count, total: progress(
                "discover",
                min(95, count / total * 100) if total else 0,
                f"Resolved {count} videos",
                True,
            ),
        )
        return store.import_urls(entries, collection=config["collection"], resolve_metadata=True)

    resource = store.one("SELECT * FROM resources WHERE id=?", (item["resource_id"],))
    if resource is None:
        raise ValueError(f"Resource {item['resource_id']} no longer exists.")
    if job["dry_run"]:
        return plan(store, resource, job["kind"], config, job["force"])
    resource_id = resource["id"]
    if job["kind"] == "metadata":
        from bilibili_audio_scraper import AudioScraper, ScraperConfig

        progress("metadata", 0, "Fetching video parts and durations…", True)
        scraper = AudioScraper(
            ScraperConfig(
                timeout=config["downloader"]["timeout"],
                retry_attempts=config["downloader"]["retries"],
            )
        )
        try:
            metadata = scraper.resolve_metadata(resource["url"])
            store.save_metadata(resource_id, metadata)
        except Exception as exc:
            # Preserve previous good metadata and every download/parse/export artifact.
            store.execute(
                "UPDATE resources SET metadata_error=? WHERE id=?", (str(exc)[:2000], resource_id)
            )
            raise
        progress("metadata", 100, "Video metadata updated", True)
        return {"part_count": metadata["part_count"], "duration": metadata["duration"]}
    stages = ["download", "parse", "export"] if job["kind"] == "pipeline" else [job["kind"]]
    result = {}
    for index, stage in enumerate(stages):

        def update(value, message, force=False):
            progress(stage, (index + value / 100) / len(stages) * 100, message, force)

        store.event(job_id, f"{resource['title']}: {stage}")
        update(0, f"Starting {stage}…", True)
        if stage == "download":
            from bilibili_audio_scraper import AudioScraper, ScraperConfig

            audio = json.loads(resource["audio"])
            if job["force"] or not available([a["path"] for a in audio]):
                folder = store.root / "audio" / str(resource_id) / f"job-{job_id}"
                scraper = AudioScraper(
                    ScraperConfig(
                        output_dir=folder,
                        timeout=config["downloader"]["timeout"],
                        retry_attempts=config["downloader"]["retries"],
                        quiet=True,
                        # A DB manifest is the cache boundary; re-extract to discover all multipart entries.
                        overwrite=True,
                    )
                )

                def hook(event):
                    total = event.get("total_bytes") or event.get("total_bytes_estimate")
                    downloaded = event.get("downloaded_bytes", 0)
                    update(
                        min(95, downloaded / total * 100) if total else 0,
                        (
                            "Converting audio…"
                            if event["status"] == "finished"
                            else f"Downloaded {downloaded / 1048576:.1f} MB"
                            + (f" of {total / 1048576:.1f} MB" if total else "")
                        ),
                    )

                audio = [
                    {**asdict(source), "path": str(source.path)}
                    for source in scraper.download_audio(resource["url"], progress=hook)
                ]
                resource["audio"] = json.dumps(audio)
                resource["title"] = (
                    json.loads(resource.get("metadata") or "{}").get("title") or audio[0]["title"]
                )
                # Downstream manifests are invalidated after a successful new download.
                resource["parsed"], resource["outputs"] = "[]", "[]"
                store.execute(
                    "UPDATE resources SET audio=?,title=?,parsed='[]',outputs='[]' WHERE id=?",
                    (resource["audio"], resource["title"], resource_id),
                )
            result[stage] = {"audio_parts": len(audio)}
        elif stage == "parse":
            audio = json.loads(resource["audio"])
            if not available([a["path"] for a in audio]):
                raise ValueError("Download this resource first, or choose Full pipeline.")
            paths = json.loads(resource["parsed"])
            if job["force"] or not available(paths):
                from .models import ParserSettings

                options = ParserSettings.model_validate(config["parser"]).model_dump()
                cloud = options["provider"] != "whisper"
                if cloud:
                    from bilibili_transcript.api_parser import DashScopeParser

                    parser = DashScopeParser(
                        model=(
                            options["asr_model"]
                            if options["provider"] == "asr_flash"
                            else "paraformer-v2"
                        ),
                        api_key=store.api_key(options["region"]),
                        region=options["region"],
                        language=options["language"],
                        timeout=options["api_timeout"],
                    )
                else:
                    from bilibili_transcript.parser import ParserConfig, WhisperParser

                    parser = WhisperParser(
                        ParserConfig(
                            model_size=options["model"],
                            device=options["device"],
                            compute_type=options["compute_type"],
                            language=None if options["language"] == "auto" else options["language"],
                            beam_size=options["beam_size"],
                            vad_filter=options["vad_filter"],
                        )
                    )
                paths = []
                update(
                    0,
                    (
                        "Preparing cloud transcription…"
                        if cloud
                        else "Loading model (first use downloads model weights)…"
                    ),
                    True,
                )
                for part, source in enumerate(audio):

                    def segment_progress(end, duration):
                        update(
                            (part + min(1, end / duration) if duration else part)
                            / len(audio)
                            * 100,
                            f"Part {part + 1}/{len(audio)} · {end:.0f}s transcribed",
                        )

                    extra = {}
                    if cloud:
                        if (source.get("duration") or 0) > 12 * 3600:
                            raise ValueError(
                                "Cloud transcription supports up to 12 hours per file. Split this recording first."
                            )
                        extra = {
                            "checkpoint": store.root
                            / "cloud-tasks"
                            / f"job-{job_id}"
                            / f"item-{item_id}-part-{part + 1}.json",
                            "status": lambda message: update(
                                part / len(audio) * 100,
                                f"Part {part + 1}/{len(audio)} · {message}",
                                True,
                            ),
                        }
                    transcript = parser.transcribe(
                        Path(source["path"]), progress=segment_progress, **extra
                    )
                    transcript.title, transcript.url = source["title"], resource["url"]
                    path = (
                        store.root
                        / "parsed"
                        / str(resource_id)
                        / f"job-{job_id}-part-{part + 1}.json"
                    )
                    atomic_json(path, {**asdict(transcript), "parser": options})
                    paths.append(str(path))
                parser.unload_model()
                resource["parsed"] = json.dumps(paths)
                resource["outputs"] = "[]"
                store.execute(
                    "UPDATE resources SET parsed=?,outputs='[]' WHERE id=?",
                    (resource["parsed"], resource_id),
                )
            result[stage] = {"transcript_parts": len(paths)}
        elif stage == "export":
            paths = json.loads(resource["parsed"])
            if not available(paths):
                raise ValueError("Parse this resource before exporting.")
            outputs = []
            for part, path in enumerate(paths, 1):
                outputs.extend(
                    export_transcript(
                        json.loads(Path(path).read_text(encoding="utf-8")),
                        store.root / "exports" / str(resource_id) / f"job-{job_id}",
                        f"part-{part}",
                        config["output"],
                    )
                )
                update(part / len(paths) * 100, f"Exported part {part}/{len(paths)}", True)
            store.execute(
                "UPDATE resources SET outputs=? WHERE id=?", (json.dumps(outputs), resource_id)
            )
            result[stage] = {"files": len(outputs)}
        update(100, f"{stage.capitalize()} complete", True)
    return result
