"""
Parser module for speech-to-text transcription.
Currently supports faster-whisper for local processing.
"""

from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import timedelta

@dataclass
class TranscriptSegment:
    """A single segment of transcript with timing."""
    start: float  # seconds
    end: float  # seconds
    text: str

    def __str__(self) -> str:
        """Format as: [HH:MM:SS --> HH:MM:SS] text"""
        start_str = self._format_time(self.start)
        end_str = self._format_time(self.end)
        return f"[{start_str} --> {end_str}] {self.text}"

    @staticmethod
    def _format_time(seconds: float) -> str:
        """Convert seconds to HH:MM:SS format."""
        td = timedelta(seconds=seconds)
        hours = int(td.total_seconds() // 3600)
        minutes = int((td.total_seconds() % 3600) // 60)
        secs = int(td.total_seconds() % 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@dataclass
class Transcript:
    """Complete transcript with metadata."""
    title: str
    url: str
    language: str
    segments: List[TranscriptSegment] = field(default_factory=list)

    def to_text(self) -> str:
        """Export as plain text without timestamps."""
        return "\n".join(segment.text for segment in self.segments)

    def to_text_with_timestamps(self) -> str:
        """Export as text with timestamps."""
        return "\n".join(str(segment) for segment in self.segments)

    def to_vtt(self) -> str:
        """Export as WebVTT format."""
        lines = ["WEBVTT\n"]
        for segment in self.segments:
            start_str = self._format_vtt_time(segment.start)
            end_str = self._format_vtt_time(segment.end)
            lines.append(f"{start_str} --> {end_str}")
            lines.append(segment.text)
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _format_vtt_time(seconds: float) -> str:
        """Convert seconds to VTT format (HH:MM:SS.mmm)."""
        td = timedelta(seconds=seconds)
        hours = int(td.total_seconds() // 3600)
        minutes = int((td.total_seconds() % 3600) // 60)
        secs = int(td.total_seconds() % 60)
        millis = int((td.total_seconds() % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


class ParserConfig:
    """Configuration for parser behavior."""

    def __init__(
        self,
        model_size: str = "base",
        device: str = "cpu",
        language: str = "zh",
        compute_type: str = "default",
        beam_size: int = 5,
        vad_filter: bool = False,
    ):
        self.model_size = model_size  # tiny, base, small, medium, large
        self.device = device  # cpu, cuda, auto
        self.language = language  # zh for Chinese
        self.compute_type = compute_type  # default, auto, int8, float16, etc
        self.beam_size = beam_size
        self.vad_filter = vad_filter


class WhisperParser:
    """Transcribes audio using faster-whisper."""

    def __init__(self, config: Optional[ParserConfig] = None):
        self.config = config or ParserConfig()
        self._model = None

    def _load_model(self):
        """Lazy load model on first use."""
        if self._model is None:
            from faster_whisper import WhisperModel
            print(
                f"Loading Whisper {self.config.model_size} model on {self.config.device}..."
            )
            self._model = WhisperModel(
                self.config.model_size,
                device=self.config.device,
                compute_type=self.config.compute_type,
            )
        return self._model

    def transcribe(self, audio_path: Path, progress=None) -> Transcript:
        """
        Transcribe audio file to text.

        Args:
            audio_path: Path to audio file

        Returns:
            Transcript object with segments and metadata

        Raises:
            FileNotFoundError: If audio file doesn't exist
            RuntimeError: If transcription fails
        """
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        try:
            model = self._load_model()

            print(f"Transcribing {audio_path.name}...")
            segments, info = model.transcribe(
                str(audio_path),
                language=self.config.language,
                beam_size=self.config.beam_size,
                vad_filter=self.config.vad_filter,
            )

            transcript = Transcript(
                title=audio_path.stem,
                url="",
                language=info.language,
            )

            for segment in segments:
                if progress:
                    progress(segment.end, info.duration)
                transcript.segments.append(
                    TranscriptSegment(
                        start=segment.start,
                        end=segment.end,
                        text=segment.text.strip(),
                    )
                )

            print(f"Transcription complete: {len(transcript.segments)} segments")
            return transcript

        except Exception as e:
            raise RuntimeError(f"Transcription failed: {e}")

    def unload_model(self):
        """Free model resources."""
        self._model = None
