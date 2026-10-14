"""
Main entry point for bilibili-transcript tool.
"""

import argparse
from pathlib import Path
import re
import requests
from typing import List
from .orchestrator import TranscriptOrchestrator
from .scraper import ScraperConfig
from .parser import ParserConfig
from .batch_processor import BatchProcessor


def get_video_urls_from_collection(collection_url: str) -> List[str]:
    """
    Extract all video URLs from a Bilibili collection/series page.

    Args:
        collection_url: URL of the collection page, e.g.,
                       https://space.bilibili.com/45502/lists/31284?type=series

    Returns:
        List of video URLs in the format https://www.bilibili.com/video/{bvid}
    """
    # Extract mid (user ID) and series_id from the URL
    # Format: https://space.bilibili.com/{mid}/lists/{series_id}?type=series
    match = re.search(r'space\.bilibili\.com/(\d+)/lists/(\d+)', collection_url)
    if not match:
        raise ValueError(f"Invalid collection URL format: {collection_url}")

    mid = match.group(1)
    series_id = match.group(2)

    print(f"Fetching collection: mid={mid}, series_id={series_id}")

    # Bilibili API endpoint for series archives
    api_url = "https://api.bilibili.com/x/series/archives"

    all_video_urls = []
    page_num = 1
    page_size = 30

    while True:
        # Make API request with browser headers
        params = {
            'mid': mid,
            'series_id': series_id,
            'pn': page_num,
            'ps': page_size,
            'sort': 'desc',
            'only_normal': 'true'
        }

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://space.bilibili.com/',
        }

        response = requests.get(api_url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()

        # Check if request was successful
        if data.get('code') != 0:
            raise RuntimeError(f"API returned error code {data.get('code')}: {data.get('message')}")

        # Extract archives from response
        archives = data.get('data', {}).get('archives', [])
        if not archives:
            break  # No more videos

        # Extract BV IDs and construct URLs
        for archive in archives:
            bvid = archive.get('bvid')
            if bvid:
                video_url = f"https://www.bilibili.com/video/{bvid}"
                all_video_urls.append(video_url)

        # Check pagination info
        page_info = data.get('data', {}).get('page', {})
        total_videos = page_info.get('total', 0)
        current_count = len(all_video_urls)

        print(f"Fetched page {page_num}: {len(archives)} videos (total so far: {current_count}/{total_videos})")

        # Check if we've fetched all videos
        if current_count >= total_videos:
            break

        page_num += 1

    print(f"\nTotal videos found: {len(all_video_urls)}")
    return all_video_urls


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
    scraper_config = ScraperConfig(quiet=False)
    parser_config = ParserConfig(
        model_size=args.model_size,
        device=args.device,
        language=args.language,
    )

    orchestrator = TranscriptOrchestrator(
        scraper_config=scraper_config,
        parser_config=parser_config,
        output_dir=args.output_dir,
    )

    try:
        if args.video:
            # Single video mode
            print("\n" + "="*60)
            print("SINGLE VIDEO MODE")
            print("="*60)
            print(f"Processing: {args.video}\n")

            transcript = orchestrator.process_video(args.video)

            print("\n" + "="*60)
            print("TRANSCRIPT COMPLETED")
            print("="*60)
            print(f"Title: {transcript.title}")
            print(f"Language: {transcript.language}")
            print(f"Segments: {len(transcript.segments)}")
            if transcript.segments:
                print(f"\nFirst segment: {transcript.segments[0].text[:100]}...")
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
                output_dir=args.output_dir,
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
