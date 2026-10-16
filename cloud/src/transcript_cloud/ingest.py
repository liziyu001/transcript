"""Local scraping and resumable OSS upload; never scrapes from a datacenter."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def duration(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = float(result.stdout.strip())
    if not 0 < value <= 43200:
        raise ValueError("Recordings must be at most 12 hours; split this file before uploading")
    return value


def upload(path, bucket, state_dir, title=None, source_url=""):
    import oss2

    size = path.stat().st_size
    if not 0 < size <= 2 * 1024**3:
        raise ValueError("Each audio file must be nonempty and at most 2 GB")
    seconds = duration(path)
    checksum = digest(path)
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in {"m4a", "mp3", "wav", "aac", "flac", "opus", "ogg", "webm", "mp4"}:
        raise ValueError("Unsupported audio extension")
    key = f"audio/{checksum}/source.{suffix}"
    manifest_path = state_dir / f"{checksum}.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    )
    try:
        remote = bucket.head_object(key)
        exists = (
            remote.content_length == size and remote.headers.get("x-oss-meta-sha256") == checksum
        )
    except oss2.exceptions.NoSuchKey:
        exists = False
    if not exists:
        checkpoints = state_dir / "multipart"
        checkpoints.mkdir(parents=True, exist_ok=True)
        oss2.resumable_upload(
            bucket,
            key,
            str(path),
            store=oss2.ResumableStore(root=str(checkpoints)),
            multipart_threshold=16 * 1024**2,
            part_size=8 * 1024**2,
            num_threads=3,
            headers={"x-oss-meta-sha256": checksum},
        )
    registration = {
        "sha256": checksum,
        "key": key,
        "size": size,
        "duration": seconds,
        "title": title or path.stem,
        "source_url": source_url,
    }
    manifest.update(audio=registration, local_path=str(path.resolve()), uploaded=True)
    atomic_json(manifest_path, manifest)
    return manifest_path, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=os.getenv("TC_API_URL"))
    parser.add_argument("--state-dir", type=Path, default=Path("data/cloud-ingest"))
    parser.add_argument(
        "--start", action="store_true", help="Also start paid recognition as each file arrives"
    )
    parser.add_argument(
        "--model",
        default="qwen-audio-3.1-asr-flash-filetrans",
        choices=[
            "paraformer-v2",
            "qwen3-asr-flash-filetrans",
            "qwen-audio-3.1-asr-flash-filetrans",
        ],
    )
    parser.add_argument("--language", default="auto", choices=["auto", "zh", "en"])
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--files", nargs="+", type=Path)
    sources.add_argument("--urls-file", type=Path)
    sources.add_argument("--collection")
    args = parser.parse_args(argv)
    token = os.getenv("TC_PANEL_TOKEN", "")
    if not args.api or len(token) < 24:
        parser.error("Set TC_API_URL and TC_PANEL_TOKEN (at least 24 characters)")
    parsed = urlparse(args.api)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
    ):
        parser.error("Cloud API must use HTTPS")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    import oss2

    from .storage import oss_auth

    with httpx.Client(
        base_url=args.api.rstrip("/"), headers={"Authorization": "Bearer " + token}, timeout=60
    ) as client:
        response = client.get("/api/config")
        response.raise_for_status()
        config = response.json()
        bucket = oss2.Bucket(
            oss_auth(), f"https://oss-{config['region']}.aliyuncs.com", config["bucket"]
        )
        failures = 0

        def process(path, title=None, url=""):
            nonlocal failures
            try:
                manifest_path, manifest = upload(path, bucket, args.state_dir, title, url)
                response = client.post("/api/audio", json=manifest["audio"])
                response.raise_for_status()
                manifest["registered"] = True
                atomic_json(manifest_path, manifest)
                if args.start:
                    options = {"model": args.model, "language": args.language}
                    # Persist request identity BEFORE sending; interrupted submission is replayable.
                    requests = manifest.setdefault("requests", {})
                    options_key = json.dumps(options, sort_keys=True)
                    request_id = requests.setdefault(options_key, uuid.uuid4().hex)
                    atomic_json(manifest_path, manifest)
                    response = client.post(
                        "/api/jobs",
                        json={
                            "audio_id": manifest["audio"]["sha256"],
                            "options": options,
                            "request_id": request_id,
                        },
                    )
                    response.raise_for_status()
                    manifest["job_id"] = response.json()["id"]
                    atomic_json(manifest_path, manifest)
                print(
                    f"Ready: {path.name}" + (f" · job {manifest['job_id']}" if args.start else "")
                )
            except Exception as exc:
                failures += 1
                # SDK exceptions can include signed requests; print only the class and file.
                print(
                    f"Failed: {path.name} ({type(exc).__name__}); rerun to resume", file=sys.stderr
                )

        if args.files:
            for path in args.files:
                process(path)
        else:
            from bilibili_audio_scraper import (
                AudioScraper,
                ScraperConfig,
                get_video_urls_from_collection,
            )

            urls = (
                get_video_urls_from_collection(args.collection)
                if args.collection
                else [
                    line.strip()
                    for line in args.urls_file.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ]
            )
            scraper = AudioScraper(ScraperConfig(output_dir=args.state_dir / "audio"))
            for url in dict.fromkeys(urls):
                try:
                    # One video at a time; the cloud starts before the entire batch uploads.
                    for source in scraper.download_audio(url):
                        process(source.path, source.title, source.url)
                except Exception as exc:
                    failures += 1
                    print(f"Scrape failed ({type(exc).__name__}): {url}", file=sys.stderr)
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
