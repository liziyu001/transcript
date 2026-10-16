"""Downloaded audio remains available after a transcription failure."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from bilibili_transcript.orchestrator import TranscriptOrchestrator
from bilibili_transcript.scraper import AudioSource


def test_failed_transcription_keeps_downloaded_audio(tmp_path: Path):
    audio = tmp_path / "BV123.m4a"
    audio.write_bytes(b"audio")
    orchestrator = TranscriptOrchestrator(output_dir=tmp_path / "transcripts")
    orchestrator.scraper.download_audio = Mock(return_value=[
        AudioSource(path=audio, url="https://www.bilibili.com/video/BV123", title="Example")
    ])
    orchestrator.parser.transcribe = Mock(side_effect=RuntimeError("model unavailable"))

    with pytest.raises(RuntimeError, match="model unavailable"):
        orchestrator.process_video("https://www.bilibili.com/video/BV123")

    assert audio.read_bytes() == b"audio"
