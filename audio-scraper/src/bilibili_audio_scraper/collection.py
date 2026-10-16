"""Fetch the video URLs in a Bilibili series."""

import re
from urllib.parse import urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def get_video_urls_from_collection(collection_url: str) -> list[str]:
    return [entry["url"] for entry in get_collection_entries(collection_url)]


def get_collection_entries(collection_url: str, timeout=30, retries=3, progress=None) -> list[dict]:
    parsed = urlparse(collection_url)
    match = re.fullmatch(r"/(\d+)/lists/(\d+)/?", parsed.path)
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname != "space.bilibili.com"
        or not match
    ):
        raise ValueError(f"Invalid Bilibili collection URL: {collection_url}")

    mid, series_id = match.groups()
    session = requests.Session()
    kind = parse_qs(parsed.query).get("type", ["series"])[0]
    if kind not in ("series", "season"):
        raise ValueError("Collection type must be series or season")
    retry = Retry(total=retries, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://space.bilibili.com/{mid}/",
    }
    entries: list[dict] = []
    seen: set[str] = set()
    page = 1
    try:
        while True:
            params = (
                {"mid": mid, "season_id": series_id, "page_num": page, "page_size": 30}
                if kind == "season"
                else {
                    "mid": mid,
                    "series_id": series_id,
                    "pn": page,
                    "ps": 30,
                    "sort": "desc",
                    "only_normal": "true",
                }
            )
            response = session.get(
                (
                    "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"
                    if kind == "season"
                    else "https://api.bilibili.com/x/series/archives"
                ),
                params=params,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(
                    f"Bilibili API error {payload.get('code')}: {payload.get('message')}"
                )
            data = payload.get("data") or {}
            archives = data.get("archives") or []
            if not archives:
                break
            before = len(entries)
            for archive in archives:
                bvid = archive.get("bvid")
                if bvid and bvid not in seen:
                    seen.add(bvid)
                    duration = archive.get("duration")
                    entries.append(
                        {
                            "url": f"https://www.bilibili.com/video/{bvid}",
                            "title": archive.get("title") or bvid,
                            "metadata": {
                                "bvid": bvid,
                                "title": archive.get("title") or bvid,
                                "duration": (
                                    duration if type(duration) is int and duration >= 0 else None
                                ),
                            },
                        }
                    )
            total = (data.get("page") or {}).get("total")
            if progress:
                progress(len(entries), total)
            if len(entries) == before:
                raise RuntimeError("Collection pagination repeated a page; retry discovery later.")
            if (total is not None and len(entries) >= int(total)) or len(archives) < 30:
                break
            page += 1
    finally:
        session.close()
    return entries
