"""
Orchestrator module that coordinates scraping and parsing.
Wires together scraper and parser components.
"""

from pathlib import Path
from typing import Optional
import json
from datetime import datetime
import hashlib

from .scraper import AudioScraper, ScraperConfig, AudioSource
from .parser import WhisperParser, ParserConfig, Transcript


class TranscriptOrchestrator:
    """Coordinates audio download and transcription."""

    def __init__(
        self,
        scraper_config: Optional[ScraperConfig] = None,
        parser_config: Optional[ParserConfig] = None,
        output_dir: Optional[Path] = None,
    ):
        self.scraper = AudioScraper(scraper_config)
        self.parser = WhisperParser(parser_config)
        self.output_dir = output_dir or Path("data/transcripts")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def process_video(self, url: str) -> list[Transcript]:
        """
        Download and transcribe all audio from a video URL.

        Args:
            url: Video URL (Bilibili livestream replay)

        Returns:
            List of Transcript objects

        Raises:
            RuntimeError: If any step fails
        """
        print(f"\n{'='*60}")
        print(f"Processing: {url}")
        print(f"{'='*60}\n")

        # Step 1: Download audio
        print("[1/3] Downloading audio...")
        audio_sources = self.scraper.download_audio(url)
        print(f"  [OK] Downloaded: {audio_sources[0].title}")
        print(f"  [OK] Found {len(audio_sources)} audio file(s)")
        if audio_sources[0].duration:
            print(f"  [OK] Duration: {audio_sources[0].duration:.1f}s")

        transcripts = []

        try:
            for part_num, audio_source in enumerate(audio_sources, 1):
                # Step 2: Transcribe
                print(f"\n[2/3] Transcribing audio (part {part_num}/{len(audio_sources)})...")
                transcript = self.parser.transcribe(audio_source.path)
                transcript.url = url
                print(f"  [OK] Transcription complete")

                # Step 3: Save results
                print(f"\n[3/3] Saving transcript...")
                url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                safe_title = self._sanitize_filename(transcript.title)
                video_dir = self.output_dir / f"{safe_title}_{url_hash}"
                self._save_transcript(transcript, part_num if len(audio_sources) > 1 else None)
                print(f"  [OK] Saved to {video_dir}")

                transcripts.append(transcript)

            print(f"\n{'='*60}")
            print(f"[OK] Complete! Processed {len(audio_sources)} audio file(s)")
            print(f"{'='*60}\n")

            return transcripts

        finally:
            # Clean up temp audio files
            for audio_source in audio_sources:
                if audio_source.path.exists():
                    audio_source.path.unlink()
            print("  [OK] Cleaned up temporary audio files")

    def _save_transcript(self, transcript: Transcript, part_num: Optional[int] = None):
        """Save transcript in multiple formats to a video-specific directory."""
        # Create directory for this video using URL hash
        url_hash = hashlib.md5(transcript.url.encode()).hexdigest()[:8]
        safe_title = self._sanitize_filename(transcript.title)
        video_dir = self.output_dir / f"{safe_title}_{url_hash}"
        video_dir.mkdir(parents=True, exist_ok=True)

        safe_title = self._sanitize_filename(transcript.title)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if part_num is not None:
            base_name = f"{safe_title}_part{part_num}_{timestamp}"
        else:
            base_name = f"{safe_title}_{timestamp}"

        # Plain text
        txt_path = video_dir / f"{base_name}.txt"
        txt_path.write_text(transcript.to_text(), encoding="utf-8")

        # Text with timestamps
        ts_path = video_dir / f"{base_name}_with_ts.txt"
        ts_path.write_text(transcript.to_text_with_timestamps(), encoding="utf-8")

        # JSON with full metadata
        json_path = video_dir / f"{base_name}.json"
        json_data = {
            "title": transcript.title,
            "url": transcript.url,
            "language": transcript.language,
            "timestamp": timestamp,
            "segment_count": len(transcript.segments),
            "segments": [
                {
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text,
                }
                for seg in transcript.segments
            ],
        }
        if part_num is not None:
            json_data["part_number"] = part_num

        json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # VTT subtitle format
        vtt_path = video_dir / f"{base_name}.vtt"
        vtt_path.write_text(transcript.to_vtt(), encoding="utf-8")

    @staticmethod
    def _sanitize_filename(name: str, max_length: int = 100) -> str:
        """Convert title to safe filename."""
        import re
        # Remove invalid characters
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        # Limit length
        return safe[:max_length].strip("._-")
