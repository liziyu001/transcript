"""Validated inputs shared by HTTP handlers and workers."""

from __future__ import annotations

import re
from typing import Literal, Optional
from urllib.parse import urlparse, parse_qs

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


def video_url(value: str) -> str:
    parsed = urlparse(value.strip())
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]+|av[0-9]+)/?", parsed.path)
    if (
        parsed.scheme not in ("https", "http")
        or parsed.hostname not in ("www.bilibili.com", "bilibili.com", "m.bilibili.com")
        or not match
        or parsed.port not in (None, 80, 443)
        or parsed.username
    ):
        raise ValueError("Use a full Bilibili video URL (https://www.bilibili.com/video/BV…).")
    return f"https://www.bilibili.com/video/{match[1]}"


def collection_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname != "space.bilibili.com"
        or not re.fullmatch(r"/\d+/lists/\d+/?", parsed.path)
        or parsed.port not in (None, 80, 443)
        or parsed.username
    ):
        raise ValueError(
            "Use a Bilibili collection URL: https://space.bilibili.com/USER/lists/ID?type=series (or season)."
        )
    kind = parse_qs(parsed.query).get("type", ["series"])[0]
    if kind not in ("series", "season"):
        raise ValueError("Collection type must be series or season.")
    return f"https://space.bilibili.com{parsed.path.rstrip('/')}?type={kind}"


class DownloaderSettings(BaseModel):
    timeout: int = Field(60, ge=5, le=600)
    retries: int = Field(3, ge=0, le=20)


class ParserSettings(BaseModel):
    provider: Literal["whisper", "asr_flash", "paraformer"] = "whisper"
    asr_model: Literal["qwen-audio-3.1-asr-flash-filetrans", "qwen3-asr-flash-filetrans"] = (
        "qwen-audio-3.1-asr-flash-filetrans"
    )
    region: Literal["beijing", "singapore"] = "beijing"
    api_timeout: int = Field(7200, ge=60, le=86400)
    # Write-only fields: never included in settings, job snapshots or exports.
    api_key: Optional[SecretStr] = Field(None, exclude=True)
    clear_api_key: bool = Field(False, exclude=True)
    model: Literal["tiny", "base", "small", "medium", "large-v3", "turbo"] = "base"
    device: Literal["cpu", "cuda", "auto"] = "cpu"
    compute_type: Literal["default", "auto", "int8", "float16", "float32"] = "int8"
    language: str = Field("zh", max_length=10)
    beam_size: int = Field(5, ge=1, le=10)
    vad_filter: bool = True

    @model_validator(mode="after")
    def check_cloud_settings(self):
        if self.provider == "paraformer" and self.region != "beijing":
            raise ValueError("Paraformer requires the Beijing region and a Beijing API key.")
        if self.api_key and any(
            ord(c) < 33 or ord(c) > 126 for c in self.api_key.get_secret_value().strip()
        ):
            raise ValueError("API key must contain only printable ASCII characters without spaces.")
        return self

    @field_validator("language")
    @classmethod
    def check_language(cls, value):
        value = value.strip().lower()
        if value != "auto" and not re.fullmatch(r"[a-z]{2,3}", value):
            raise ValueError("Use auto or a language code such as zh, en, ja.")
        return value


class OutputSettings(BaseModel):
    formats: list[Literal["txt", "vtt", "srt", "json"]] = Field(
        default_factory=lambda: ["txt", "vtt", "json"], min_length=1
    )
    timestamps: bool = False


class Settings(BaseModel):
    downloader: DownloaderSettings = Field(default_factory=DownloaderSettings)
    parser: ParserSettings = Field(default_factory=ParserSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)


class ImportLinks(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=5000)

    @field_validator("urls")
    @classmethod
    def normalize(cls, urls):
        return list(dict.fromkeys(video_url(url) for url in urls))


class ImportCollection(BaseModel):
    url: str
    name: str = Field(min_length=1, max_length=120)
    dry_run: bool = False

    @field_validator("url")
    @classmethod
    def normalize(cls, url):
        return collection_url(url)


class NewJob(BaseModel):
    kind: Literal["download", "parse", "export", "pipeline"]
    resource_ids: list[int] = Field(min_length=1, max_length=5000)
    dry_run: bool = False
    force: bool = False
