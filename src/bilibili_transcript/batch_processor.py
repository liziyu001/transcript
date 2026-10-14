"""
Batch processor for handling collections of videos with resume capability.
"""

import json
import signal
import sys
from pathlib import Path
from typing import List, Optional
from datetime import datetime


class BatchProgress:
    """Tracks progress of batch processing with persistence."""

    def __init__(self, progress_file: Path):
        self.progress_file = progress_file
        self.data = self._load()

    def _load(self) -> dict:
        """Load progress from file or create new."""
        if self.progress_file.exists():
            with open(self.progress_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {
            'collection_url': None,
            'total_videos': 0,
            'completed': [],
            'failed': [],
            'skipped': [],
            'last_updated': None,
        }

    def _save(self):
        """Save progress to file."""
        self.data['last_updated'] = datetime.now().isoformat()
        self.progress_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.progress_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def initialize(self, collection_url: str, video_urls: List[str]):
        """Initialize a new batch or verify existing one."""
        if self.data['collection_url'] and self.data['collection_url'] != collection_url:
            raise ValueError(
                f"Progress file exists for different collection.\n"
                f"  Existing: {self.data['collection_url']}\n"
                f"  Requested: {collection_url}\n"
                f"Delete {self.progress_file} to start fresh."
            )

        self.data['collection_url'] = collection_url
        self.data['total_videos'] = len(video_urls)
        self._save()

    def mark_completed(self, video_url: str):
        """Mark a video as successfully processed."""
        if video_url not in self.data['completed']:
            self.data['completed'].append(video_url)
            self._save()

    def mark_failed(self, video_url: str, error: str):
        """Mark a video as failed with error message."""
        entry = {'url': video_url, 'error': error, 'timestamp': datetime.now().isoformat()}
        if entry not in self.data['failed']:
            self.data['failed'].append(entry)
            self._save()

    def mark_skipped(self, video_url: str, reason: str):
        """Mark a video as skipped."""
        entry = {'url': video_url, 'reason': reason}
        if entry not in self.data['skipped']:
            self.data['skipped'].append(entry)
            self._save()

    def is_processed(self, video_url: str) -> bool:
        """Check if video has already been processed."""
        return video_url in self.data['completed']

    def get_pending_urls(self, all_urls: List[str]) -> List[str]:
        """Get list of URLs that haven't been completed yet."""
        completed_set = set(self.data['completed'])
        return [url for url in all_urls if url not in completed_set]

    def get_stats(self) -> dict:
        """Get processing statistics."""
        return {
            'total': self.data['total_videos'],
            'completed': len(self.data['completed']),
            'failed': len(self.data['failed']),
            'skipped': len(self.data['skipped']),
            'pending': self.data['total_videos'] - len(self.data['completed']),
        }


class BatchProcessor:
    """Process multiple videos with progress tracking and resume capability."""

    def __init__(self, orchestrator, output_dir: Path, collection_url: str):
        self.orchestrator = orchestrator
        self.output_dir = output_dir
        self.collection_url = collection_url
        self.progress = BatchProgress(output_dir / '.batch_progress.json')
        self.cancelled = False

        # Set up signal handlers for graceful cancellation
        signal.signal(signal.SIGINT, self._handle_cancel)
        signal.signal(signal.SIGTERM, self._handle_cancel)

    def _handle_cancel(self, signum, frame):
        """Handle cancellation signal."""
        print("\n\n" + "="*60)
        print("CANCELLATION REQUESTED")
        print("="*60)
        print("Finishing current video, then stopping...")
        print("Progress has been saved. Run again to resume.")
        print("="*60 + "\n")
        self.cancelled = True

    def process_collection(self, video_urls: List[str], skip_existing: bool = True):
        """
        Process all videos in a collection.

        Args:
            video_urls: List of video URLs to process
            skip_existing: Skip videos that already have transcript files
        """
        # Initialize or verify progress
        self.progress.initialize(self.collection_url, video_urls)

        # Get pending URLs
        pending_urls = self.progress.get_pending_urls(video_urls)

        if not pending_urls:
            print("\n" + "="*60)
            print("ALL VIDEOS ALREADY PROCESSED")
            print("="*60)
            self._print_stats()
            return

        print("\n" + "="*60)
        print("BATCH PROCESSING")
        print("="*60)
        print(f"Collection: {self.collection_url}")
        print(f"Total videos: {len(video_urls)}")
        print(f"Already completed: {len(self.progress.data['completed'])}")
        print(f"To process: {len(pending_urls)}")
        print("="*60)
        print("\nPress Ctrl+C to cancel (will finish current video first)\n")

        # Process each video
        for i, video_url in enumerate(pending_urls, 1):
            if self.cancelled:
                print("\nBatch processing cancelled by user.")
                break

            print("\n" + "-"*60)
            print(f"[{i}/{len(pending_urls)}] Processing: {video_url}")
            print("-"*60)

            try:
                # Check if output already exists
                if skip_existing:
                    # Check for existing transcript files
                    if self._has_existing_transcript(video_url):
                        print("  → Transcript already exists, skipping")
                        self.progress.mark_skipped(video_url, "transcript_exists")
                        continue

                # Process the video
                transcript = self.orchestrator.process_video(video_url)

                # Mark as completed
                self.progress.mark_completed(video_url)

                print(f"  ✓ Completed: {transcript.title}")
                print(f"  ✓ Segments: {len(transcript.segments)}")

            except KeyboardInterrupt:
                # Re-raise to trigger cancellation handler
                raise

            except Exception as e:
                error_msg = str(e)
                print(f"  ✗ Failed: {error_msg}")
                self.progress.mark_failed(video_url, error_msg)
                # Continue with next video

            # Print progress
            self._print_stats()

        print("\n" + "="*60)
        print("BATCH PROCESSING COMPLETE" if not self.cancelled else "BATCH PROCESSING PAUSED")
        print("="*60)
        self._print_stats()
        print("="*60 + "\n")

    def _has_existing_transcript(self, video_url: str) -> bool:
        """Check if transcript files already exist for this video."""
        # Extract video ID from URL
        import re
        match = re.search(r'/video/([A-Za-z0-9]+)', video_url)
        if not match:
            return False

        video_id = match.group(1)

        # Check for transcript files in output directory
        # The orchestrator creates files like: {output_dir}/{video_id}/transcript.json
        video_dir = self.output_dir / video_id
        transcript_file = video_dir / 'transcript.json'

        return transcript_file.exists()

    def _print_stats(self):
        """Print current processing statistics."""
        stats = self.progress.get_stats()
        completed_pct = (stats['completed'] / stats['total'] * 100) if stats['total'] > 0 else 0

        print(f"\n📊 Progress: {stats['completed']}/{stats['total']} "
              f"({completed_pct:.1f}%) | "
              f"Failed: {stats['failed']} | "
              f"Skipped: {stats['skipped']}")

