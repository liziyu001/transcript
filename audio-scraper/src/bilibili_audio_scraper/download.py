"""Download audio with yt-dlp into a caller-selected directory."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import re

from yt_dlp import YoutubeDL


@dataclass
class ScraperConfig:
    output_dir: Path = Path("data/audio")
    timeout: int = 60
    retry_attempts: int = 3
    quiet: bool = False
    overwrite: bool = False


@dataclass
class AudioSource:
    path: Path
    url: str
    title: str
    duration: Optional[float] = None


def _video_entries(info: dict) -> list[dict]:
    if info.get("entries") is not None:
        return [leaf for entry in info["entries"] if entry for leaf in _video_entries(entry)]
    return [info]


class AudioScraper:
    """Download one video URL, including any entries returned by its extractor."""

    def __init__(self, config: Optional[ScraperConfig] = None):
        self.config = config or ScraperConfig()

    def download_audio(self, url: str, progress=None) -> list[AudioSource]:
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        match = re.search(r"bilibili\.com/video/(BV[0-9A-Za-z]+)", url)
        if match and not self.config.overwrite:
            existing = output_dir / f"{match.group(1)}.m4a"
            if existing.is_file() and existing.stat().st_size > 0:
                return [AudioSource(path=existing, url=url, title=match.group(1))]
        options = {
            "progress_hooks": [progress] if progress else [],
            "format": "bestaudio/best",
            "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
            "socket_timeout": self.config.timeout,
            "retries": self.config.retry_attempts,
            "fragment_retries": self.config.retry_attempts,
            "extractor_retries": self.config.retry_attempts,
            "continuedl": True,
            "overwrites": self.config.overwrite,
            "quiet": self.config.quiet,
            "no_warnings": self.config.quiet,
            "noplaylist": False,
        }
        try:
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
        except Exception as exc:
            raise RuntimeError(f"Audio download failed for {url}: {exc}") from exc

        if not info:
            raise RuntimeError(f"No audio information returned for {url}")
        sources = []
        for entry in _video_entries(info):
            video_id = entry.get("id")
            if not video_id:
                raise RuntimeError(f"Downloaded entry has no video ID: {url}")
            audio_path = output_dir / f"{video_id}.m4a"
            if not audio_path.is_file():
                raise RuntimeError(f"Audio file missing after download: {audio_path}")
            sources.append(AudioSource(
                path=audio_path,
                url=entry.get("webpage_url") or url,
                title=entry.get("title") or video_id,
                duration=entry.get("duration"),
            ))
        if not sources:
            raise RuntimeError(f"No audio files downloaded for {url}")
        return sources
