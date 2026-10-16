"""
Main entry point for bilibili-transcript tool.
"""

import argparse
from pathlib import Path
import re
from bilibili_audio_scraper import get_video_urls_from_collection
from .orchestrator import TranscriptOrchestrator
from .scraper import ScraperConfig
from .parser import ParserConfig
from .batch_processor import BatchProcessor


def main():
    """
    Main entry point - supports single video or collection processing.
    """
    parser = argparse.ArgumentParser(
        description='Transcribe Bilibili videos or collections',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process a single video
  python -m bilibili_transcript.main --video "https://www.bilibili.com/video/BV1xx411c7mD"

  # Process entire collection
  python -m bilibili_transcript.main --collection "https://space.bilibili.com/45502/lists/31284?type=series"

  # Resume interrupted collection processing
  python -m bilibili_transcript.main --collection "https://space.bilibili.com/45502/lists/31284?type=series"

  # Force reprocess even if transcripts exist
  python -m bilibili_transcript.main --collection "https://space.bilibili.com/45502/lists/31284?type=series" --no-skip-existing
        """
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--video', type=str, help='Single video URL to process')
    group.add_argument('--collection', type=str, help='Collection/series URL to process')

    parser.add_argument('--output-dir', type=Path, default=Path('data/transcripts'),
                        help='Output directory for transcripts (default: data/transcripts)')
    parser.add_argument('--model-size', type=str, default='base',
                        choices=['tiny', 'base', 'small', 'medium', 'large'],
                        help='Whisper model size (default: base)')
    parser.add_argument('--device', type=str, default='cpu',
                        choices=['cpu', 'cuda'],
                        help='Device to use for inference (default: cpu)')
    parser.add_argument('--language', type=str, default='zh',
                        help='Language code (default: zh for Chinese)')
    parser.add_argument('--no-skip-existing', action='store_true',
                        help='Reprocess videos even if transcripts already exist')

    args = parser.parse_args()

    # Initialize components
    scraper_config = ScraperConfig(quiet=False, output_dir=args.output_dir / ".audio_cache")
    parser_config = ParserConfig(
        model_size=args.model_size,
        device=args.device,
        language=args.language,
    )

    # Determine collection name if processing collection
    collection_name = None
    if args.collection:
        import hashlib
        # Use collection URL hash as directory name
        collection_hash = hashlib.md5(args.collection.encode()).hexdigest()[:8]
        # Extract series_id for better readability
        match = re.search(r'lists/(\d+)', args.collection)
        series_id = match.group(1) if match else "unknown"
        collection_name = f"collection_{series_id}_{collection_hash}"

    orchestrator = TranscriptOrchestrator(
        scraper_config=scraper_config,
        parser_config=parser_config,
        output_dir=args.output_dir,
        collection_name=collection_name,
    )

    try:
        if args.video:
            # Single video mode
            print("\n" + "="*60)
            print("SINGLE VIDEO MODE")
            print("="*60)
            print(f"Processing: {args.video}\n")

            transcripts = orchestrator.process_video(args.video)

            print("\n" + "="*60)
            print("TRANSCRIPT COMPLETED")
            print("="*60)
            print(f"Title: {transcripts[0].title if transcripts else 'Unknown'}")
            print(f"Language: {transcripts[0].language if transcripts else 'Unknown'}")
            print(f"Total parts: {len(transcripts)}")
            if transcripts and transcripts[0].segments:
                print(f"\nFirst segment: {transcripts[0].segments[0].text[:100]}...")
            print("="*60)

        else:
            # Collection mode
            print("\n" + "="*60)
            print("COLLECTION MODE")
            print("="*60)
            print(f"Fetching videos from: {args.collection}\n")

            # Fetch all video URLs
            video_urls = get_video_urls_from_collection(args.collection)

            print(f"\nFound {len(video_urls)} videos")

            # Initialize batch processor
            batch_processor = BatchProcessor(
                orchestrator=orchestrator,
                collection_dir=orchestrator.output_dir,
                collection_url=args.collection
            )

            # Process the collection
            batch_processor.process_collection(
                video_urls=video_urls,
                skip_existing=not args.no_skip_existing
            )

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        return 130

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
