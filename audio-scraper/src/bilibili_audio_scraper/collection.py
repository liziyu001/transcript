"""Fetch the video URLs in a Bilibili series."""

import re
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def get_video_urls_from_collection(collection_url: str) -> list[str]:
    parsed = urlparse(collection_url)
    match = re.fullmatch(r"/(\d+)/lists/(\d+)/?", parsed.path)
    if parsed.scheme not in ("http", "https") or parsed.hostname != "space.bilibili.com" or not match:
        raise ValueError(f"Invalid Bilibili collection URL: {collection_url}")

    mid, series_id = match.groups()
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": f"https://space.bilibili.com/{mid}/",
    }
    urls: list[str] = []
    seen: set[str] = set()
    page = 1
    try:
        while True:
            response = session.get(
                "https://api.bilibili.com/x/series/archives",
                params={"mid": mid, "series_id": series_id, "pn": page, "ps": 30,
                        "sort": "desc", "only_normal": "true"},
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"Bilibili API error {payload.get('code')}: {payload.get('message')}")
            data = payload.get("data") or {}
            archives = data.get("archives") or []
            if not archives:
                break
            for archive in archives:
                bvid = archive.get("bvid")
                if bvid and bvid not in seen:
                    seen.add(bvid)
                    urls.append(f"https://www.bilibili.com/video/{bvid}")
            total = (data.get("page") or {}).get("total")
            if (total is not None and len(urls) >= int(total)) or len(archives) < 30:
                break
            page += 1
    finally:
        session.close()
    return urls
