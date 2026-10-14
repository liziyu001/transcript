"""Bilibili Transcript - Transcribe Bilibili livestream recordings to text."""

__version__ = "0.1.0"

from .scraper import AudioScraper, ScraperConfig, AudioSource
from .parser import WhisperParser, ParserConfig, Transcript, TranscriptSegment
from .orchestrator import TranscriptOrchestrator

__all__ = [
    "AudioScraper",
    "ScraperConfig",
    "AudioSource",
    "WhisperParser",
    "ParserConfig",
    "Transcript",
    "TranscriptSegment",
    "TranscriptOrchestrator",
]
