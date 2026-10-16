"""Resolve video and part metadata using JSON only, without fetching media."""

import re
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _duration(value):
    # Unknown is not zero. Do not mistake a missing duration for an empty video.
    return value if type(value) is int and value >= 0 else None


def parse_video_metadata(data):
    """Normalize x/web-interface/view data, summing all parts rather than P1."""
    raw_pages = data.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise RuntimeError("Bilibili returned no video parts.")
    pages = []
    for item in raw_pages:
        page = item.get("page")
        if type(page) is not int or page < 1:
            raise RuntimeError("Bilibili returned an invalid part number.")
        pages.append(
            {
                "page": page,
                "cid": item.get("cid"),
                "title": str(item.get("part") or f"P{page}"),
                "duration": _duration(item.get("duration")),
            }
        )
    pages.sort(key=lambda part: part["page"])
    if [part["page"] for part in pages] != list(range(1, len(pages) + 1)):
        raise RuntimeError("Bilibili returned an incomplete part list.")
    if data.get("videos") is not None and data["videos"] != len(pages):
        raise RuntimeError("Bilibili returned an incomplete part list.")
    duration = (
        sum(part["duration"] for part in pages)
        if all(part["duration"] is not None for part in pages)
        else _duration(data.get("duration"))
    )
    return {
        "bvid": data.get("bvid"),
        "title": data.get("title"),
        "part_count": len(pages),
        "duration": duration,
        "pages": pages,
    }


def get_video_metadata(url, timeout=30, retries=3):
    """Get title, total duration and every P's title/CID/duration for BV or av URLs."""
    parsed = urlparse(url.strip())
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]+|av[0-9]+)/?", parsed.path)
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname not in ("www.bilibili.com", "bilibili.com", "m.bilibili.com")
        or parsed.username
        or parsed.port not in (None, 80, 443)
        or not match
    ):
        raise ValueError("Use a full Bilibili video URL to resolve metadata.")
    video_id = match[1]
    params = {"bvid": video_id} if video_id.startswith("BV") else {"aid": video_id[2:]}
    with requests.Session() as session:
        session.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=retries,
                    backoff_factor=1,
                    status_forcelist=[429, 500, 502, 503, 504],
                )
            ),
        )
        try:
            response = session.get(
                "https://api.bilibili.com/x/web-interface/view",
                params=params,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(
                "Could not fetch Bilibili metadata. Retry metadata refresh later."
            ) from exc
    if payload.get("code") != 0:
        raise RuntimeError(
            f"Bilibili metadata error {payload.get('code')}: {payload.get('message')}"
        )
    return parse_video_metadata(payload.get("data") or {})
