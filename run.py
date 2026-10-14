#!/usr/bin/env python3
"""
Simple runner script to process a Bilibili livestream recording.
Edit VIDEO_URL to point to your target video.
"""

from pathlib import Path
from src.bilibili_transcript import TranscriptOrchestrator
from src.bilibili_transcript.scraper import ScraperConfig
from src.bilibili_transcript.parser import ParserConfig


# ==============================================================================
# CONFIGURATION - Edit this section
# ==============================================================================

# Paste your Bilibili livestream replay URL here
VIDEO_URL = "https://www.bilibili.com/video/BV1SbpQ69EVW/"  # EDIT THIS

# Output directory for transcripts
OUTPUT_DIR = Path("data/transcripts")

# Model configuration
MODEL_SIZE = "base"  # Options: tiny, base, small, medium, large
DEVICE = "cpu"  # Options: cpu, cuda, auto
LANGUAGE = "zh"  # Chinese

# ==============================================================================


def main():
    print("Bilibili Transcript Generator")
    print("=" * 60)

    scraper_config = ScraperConfig(quiet=False, output_dir=OUTPUT_DIR / ".audio_cache")
    parser_config = ParserConfig(
        model_size=MODEL_SIZE,
        device=DEVICE,
        language=LANGUAGE,
    )

    orchestrator = TranscriptOrchestrator(
        scraper_config=scraper_config,
        parser_config=parser_config,
        output_dir=OUTPUT_DIR,
    )

    try:
        transcripts = orchestrator.process_video(VIDEO_URL)

        print("\n" + "=" * 60)
        print("RESULT")
        print("=" * 60)
        print(f"[OK] Transcription saved to: {OUTPUT_DIR}")

        for i, transcript in enumerate(transcripts, 1):
            suffix = f" (part {i})" if len(transcripts) > 1 else ""
            print(f"\n[Part {i}] {transcript.title}{suffix}")
            print(f"  - Plain text: {transcript.title}_*.txt")
            print(f"  - With timestamps: {transcript.title}_*_with_ts.txt")
            print(f"  - Full metadata: {transcript.title}_*.json")
            print(f"  - VTT subtitles: {transcript.title}_*.vtt")
            print(f"  - Segments: {len(transcript.segments)}")
            if transcript.segments:
                total_secs = transcript.segments[-1].end if transcript.segments else 0
                mins = int(total_secs // 60)
                print(f"  - Duration: ~{mins} minutes")

        return 0

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
