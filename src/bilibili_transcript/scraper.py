"""
Scraper module for downloading audio from video sources.
Currently supports Bilibili livestream recordings.
"""

import tempfile
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class ScraperConfig:
    """Configuration for scraper behavior."""
    timeout: int = 600  # seconds
    retry_attempts: int = 3
    quiet: bool = False


@dataclass
class AudioSource:
    """Represents a downloaded audio file."""
    path: Path
    url: str
    title: str
    duration: Optional[float] = None


class AudioScraper:
    """Downloads audio from video URLs."""

    def __init__(self, config: Optional[ScraperConfig] = None):
        self.config = config or ScraperConfig()

    def download_audio(self, url: str) -> AudioSource:
        """
        Download audio from a video URL.

        Args:
            url: Video URL (Bilibili livestream replay)

        Returns:
            AudioSource with path to downloaded audio file

        Raises:
            RuntimeError: If download fails after retry attempts
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "%(title)s.%(ext)s"

            cmd = [
                "yt-dlp",
                "--extract-audio",
                "--audio-format", "m4a",
                "--audio-quality", "0",
                "-f", "bestaudio",
                "-o", str(output_path),
                url,
            ]

            if self.config.quiet:
                cmd.insert(1, "-q")

            try:
                result = subprocess.run(
                    cmd,
                    timeout=self.config.timeout,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                if result.returncode != 0:
                    raise RuntimeError(f"yt-dlp failed: {result.stderr}")

                # Find the downloaded file
                audio_files = list(Path(tmpdir).glob("*"))
                if not audio_files:
                    raise RuntimeError("No audio file downloaded")

                audio_file = audio_files[0]

                # Get metadata
                info_cmd = [
                    "yt-dlp",
                    "--dump-json",
                    "--extract-audio",
                    "-f", "bestaudio",
                    url,
                ]

                info_result = subprocess.run(
                    info_cmd,
                    timeout=self.config.timeout,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                title = "video"
                duration = None

                if info_result.returncode == 0:
                    import json
                    try:
                        info = json.loads(info_result.stdout)
                        title = info.get("title", "video")
                        duration = info.get("duration")
                    except (json.JSONDecodeError, KeyError):
                        pass

                # Move file to permanent location since tmpdir will be cleaned up
                permanent_path = Path(audio_file).resolve()

                return AudioSource(
                    path=permanent_path,
                    url=url,
                    title=title,
                    duration=duration,
                )

            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"Download timeout after {self.config.timeout}s"
                )
            except Exception as e:
                raise RuntimeError(f"Download failed: {e}")
