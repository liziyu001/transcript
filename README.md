# Bilibili Transcript

Transcribe Bilibili videos and livestream recordings to text using Whisper.

## Features

- **Single Video Mode**: Process individual video URLs
- **Collection Mode**: Batch process entire collections/series
- **Resume Support**: Automatically resumes interrupted batch processing
- **Progress Tracking**: Persistent progress file tracks completed, failed, and skipped videos
- **Graceful Cancellation**: Press Ctrl+C to stop after current video completes
- **Smart Skipping**: Automatically skips videos with existing transcripts

## Installation

This project uses [uv](https://github.com/astral-sh/uv) for dependency management.

```bash
# Clone the repository
git clone <repository-url>
cd transcript

# Install dependencies with uv
uv sync

# Or manually install
uv pip install -e .
```

## Usage

### Single Video

Process one video at a time:

```bash
uv run python -m bilibili_transcript.main --video "https://www.bilibili.com/video/BV1xx411c7mD"
```

### Collection Processing

Process all videos in a collection/series:

```bash
uv run python -m bilibili_transcript.main --collection "https://space.bilibili.com/45502/lists/31284?type=series"
```

The tool will:
1. Fetch all video URLs from the collection (handles pagination automatically)
2. Process each video sequentially
3. Save progress after each video
4. Skip videos that already have transcripts (unless `--no-skip-existing` is used)

### Resume Interrupted Processing

If processing is interrupted (Ctrl+C, error, or system shutdown), simply run the same command again:

```bash
uv run python -m bilibili_transcript.main --collection "https://space.bilibili.com/45502/lists/31284?type=series"
```

The tool will:
- Load the progress file (`.batch_progress.json`)
- Skip already-completed videos
- Continue from where it left off

### Options

```
--output-dir PATH          Output directory (default: data/transcripts)
--model-size SIZE          Whisper model: tiny, base, small, medium, large (default: base)
--device DEVICE            Device: cpu, cuda (default: cpu)
--language LANG            Language code (default: zh)
--no-skip-existing         Reprocess videos even if transcripts exist
```

## Output Structure

Transcripts are organized by video:

```
data/transcripts/
├── .batch_progress.json          # Progress tracking (collection mode only)
├── BV1xx411c7mD/                 # Video directory
│   ├── transcript.json           # Structured transcript data
│   ├── transcript.txt            # Plain text transcript
│   └── transcript.vtt            # WebVTT subtitle format
└── BV1nWpz6zE4U/
    ├── transcript.json
    ├── transcript.txt
    └── transcript.vtt
```

## Progress File

In collection mode, a `.batch_progress.json` file tracks:
- `collection_url`: The collection being processed
- `completed`: List of successfully processed video URLs
- `failed`: List of failed videos with error messages
- `skipped`: List of skipped videos with reasons
- `last_updated`: Timestamp of last update

Example:
```json
{
  "collection_url": "https://space.bilibili.com/45502/lists/31284?type=series",
  "total_videos": 1349,
  "completed": [
    "https://www.bilibili.com/video/BV1nWpz6zE4U",
    "https://www.bilibili.com/video/BV1SmHy6eEbg"
  ],
  "failed": [
    {
      "url": "https://www.bilibili.com/video/BV1xxx",
      "error": "Download failed",
      "timestamp": "2026-10-14T10:30:00"
    }
  ],
  "skipped": [],
  "last_updated": "2026-10-14T10:35:00"
}
```

## Cancellation

Press **Ctrl+C** once to trigger graceful shutdown:
- Current video will finish processing
- Progress is saved automatically
- Run the same command to resume

## Examples

```bash
# Process collection with default settings (base model, CPU)
uv run python -m bilibili_transcript.main --collection "https://space.bilibili.com/45502/lists/31284?type=series"

# Use larger model for better accuracy
uv run python -m bilibili_transcript.main \
  --collection "https://space.bilibili.com/45502/lists/31284?type=series" \
  --model-size large

# Use GPU acceleration with larger model
uv run python -m bilibili_transcript.main \
  --collection "https://space.bilibili.com/45502/lists/31284?type=series" \
  --device cuda \
  --model-size large

# Force reprocess everything (ignore existing transcripts)
uv run python -m bilibili_transcript.main \
  --collection "https://space.bilibili.com/45502/lists/31284?type=series" \
  --no-skip-existing

# Process single video with custom output directory
uv run python -m bilibili_transcript.main \
  --video "https://www.bilibili.com/video/BV1xx411c7mD" \
  --output-dir ./my_transcripts
```

## Requirements

- Python >= 3.9
- yt-dlp (for video download)
- faster-whisper (for transcription)
- ffmpeg (for audio processing)

## License

MIT
