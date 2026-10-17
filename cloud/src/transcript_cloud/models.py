"""Small, versioned contracts shared by the CLI, API, and worker."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AudioRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    key: str = Field(pattern=r"^audio/[a-f0-9]{64}/source\.[a-z0-9]{1,8}$")
    title: str = Field(min_length=1, max_length=500)
    source_url: str = Field(default="", max_length=2000)
    size: int = Field(gt=0, le=2 * 1024**3)
    duration: float | None = Field(default=None, gt=0, le=43200)


class ParseOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal[
        "paraformer-v2", "qwen3-asr-flash-filetrans", "qwen-audio-3.1-asr-flash-filetrans"
    ] = "qwen-audio-3.1-asr-flash-filetrans"
    language: Literal["auto", "zh", "en", "ja", "ko", "de", "fr", "ru", "es"] = "auto"


class NewJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    options: ParseOptions = Field(default_factory=ParseOptions)
    # A new request ID explicitly requests a new paid recognition; retries reuse this ID.
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    output: "OutputOptions" = Field(default_factory=lambda: OutputOptions())


class OutputOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    formats: list[Literal["txt", "srt", "vtt", "json"]] = Field(
        default_factory=lambda: ["txt", "srt", "vtt", "json"], min_length=1, max_length=4
    )
    timestamps: bool = False


class DownloadOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout: int = Field(default=60, ge=5, le=600)
    retries: int = Field(default=3, ge=0, le=20)


class StudioSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    downloader: DownloadOptions = Field(default_factory=DownloadOptions)
    parser: ParseOptions = Field(default_factory=ParseOptions)
    output: OutputOptions = Field(default_factory=OutputOptions)
    processing_concurrency: int = Field(default=4, ge=1, le=16)


NewJob.model_rebuild()


class Recovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,200}$")


class Dismissal(BaseModel):
    checked_provider: Literal[True]


class Settings(BaseModel):
    region: str = "cn-beijing"
    bucket: str
    ots_instance: str
    table: str = "transcript_records"
    flow: str
    panel_token: str = Field(min_length=24)
    dashscope_key: str = ""
    allowed_origin: str = "http://localhost:8787"
    max_active: int = Field(default=4, ge=1, le=16)
    job_timeout: int = 24 * 3600

    @classmethod
    def from_env(cls):
        import os

        return cls(
            **{
                name: os.environ["TC_" + name.upper()]
                for name in cls.model_fields
                if "TC_" + name.upper() in os.environ
            }
        )
