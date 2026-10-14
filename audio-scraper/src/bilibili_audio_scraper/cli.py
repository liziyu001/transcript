"""Command line entry point for listing and downloading audio."""

import argparse
import sys
from pathlib import Path

from .collection import get_video_urls_from_collection
from .download import AudioScraper, ScraperConfig


def _read_urls(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def main(argv: list[str] = None) -> int:
    parser = argparse.ArgumentParser(description="List Bilibili collection videos or download audio")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List video URLs in a collection without downloading")
    listing.add_argument("collection", help="Bilibili collection URL")
    listing.add_argument("--output", type=Path, help="Write URLs to a text file")

    download = commands.add_parser("download", help="Download audio from videos or a collection")
    sources = download.add_mutually_exclusive_group(required=True)
    sources.add_argument("--video", action="append", help="Video URL; repeat for multiple videos")
    sources.add_argument("--urls-file", type=Path, help="Text file with one video URL per line")
    sources.add_argument("--collection", help="Bilibili collection URL")
    download.add_argument("--output-dir", type=Path, default=Path("data/audio"))
    download.add_argument("--force", action="store_true", help="Overwrite existing audio")
    download.add_argument("--quiet", action="store_true")
    download.add_argument("--timeout", type=int, default=60, help="Network timeout in seconds")
    download.add_argument("--retries", type=int, default=3, help="Retries for each network operation")

    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            urls = get_video_urls_from_collection(args.collection)
            content = "\n".join(urls) + ("\n" if urls else "")
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(content, encoding="utf-8")
                print(f"Wrote {len(urls)} video URLs to {args.output}", file=sys.stderr)
            else:
                print(content, end="")
            return 0

        urls = (args.video if args.video else
                _read_urls(args.urls_file) if args.urls_file else
                get_video_urls_from_collection(args.collection))
        urls = list(dict.fromkeys(urls))
        if not urls:
            print("No video URLs found", file=sys.stderr)
            return 1
        if args.timeout < 1 or args.retries < 0:
            raise ValueError("--timeout must be positive and --retries cannot be negative")
        scraper = AudioScraper(ScraperConfig(
            output_dir=args.output_dir, overwrite=args.force, quiet=args.quiet,
            timeout=args.timeout, retry_attempts=args.retries,
        ))
        failures = 0
        for index, url in enumerate(urls, 1):
            try:
                for source in scraper.download_audio(url):
                    print(f"[{index}/{len(urls)}] {source.path}")
            except Exception as exc:
                failures += 1
                print(f"[{index}/{len(urls)}] {exc}", file=sys.stderr)
        print(f"Finished: {len(urls) - failures} succeeded, {failures} failed", file=sys.stderr)
        return 1 if failures else 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
