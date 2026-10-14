"""
Main entry point for bilibili-transcript tool.
"""

from pathlib import Path
from .orchestrator import TranscriptOrchestrator
from .scraper import ScraperConfig
from .parser import ParserConfig


def main():
    """
    Main entry point - process a single hardcoded video for demonstration.
    """

    # Configuration for this demo
    VIDEO_URL = "https://www.bilibili.com/video/BV1xx411c7mD/"  # Example - replace with actual replay

    # Initialize components with base model for quick demo
    scraper_config = ScraperConfig(quiet=False)
    parser_config = ParserConfig(
        model_size="base",  # Using base model as requested
        device="cpu",
        language="zh",
    )

    orchestrator = TranscriptOrchestrator(
        scraper_config=scraper_config,
        parser_config=parser_config,
        output_dir=Path("data/transcripts"),
    )

    try:
        # Process the video
        transcript = orchestrator.process_video(VIDEO_URL)

        # Print summary
        print("\n" + "="*60)
        print("TRANSCRIPT SUMMARY")
        print("="*60)
        print(f"Title: {transcript.title}")
        print(f"Language: {transcript.language}")
        print(f"Segments: {len(transcript.segments)}")
        if transcript.segments:
            print(f"\nFirst segment: {transcript.segments[0].text[:100]}...")
        print("="*60)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
