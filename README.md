# Transcript Studio

A local resource library and job-control website for Bilibili audio and transcripts.
The existing command-line tools remain available below.

## Start with Docker Compose

```bash
docker compose up -d --build
```

Open **http://localhost:8000**. No npm build or local Python/FFmpeg installation is
needed for Docker. The first build needs access to Docker Hub, GHCR, PyPI, and the
Debian package repositories. The first transcription with each model downloads
weights from Hugging Face; subsequent jobs reuse the persistent model cache.

The supplied image uses **CPU + int8**. Start with `tiny` or `base`; larger models
use substantially more memory. Selecting CUDA in the UI is intended for a local
environment with compatible NVIDIA libraries; the supplied image does not include
a CUDA runtime or GPU Compose configuration.

```bash
docker compose logs -f studio   # application logs
docker compose down            # stop; library and files are retained
docker compose up -d --build    # rebuild after updating the checkout
```

Set `STUDIO_PORT` in your environment or a local `.env` file to change port 8000.
The service binds to your computer's loopback address. It is a personal tool with
no login system; keep that binding for personal use.

### Everyday workflow

1. **Add resources → Paste video links:** enter one full Bilibili video URL per
   line. Records go into **General**. Share parameters are removed and duplicate
   URLs are deduplicated. Multipart videos are handled as one resource containing
   multiple audio/transcript parts. Short links are not supported; paste their
   expanded `/video/BV…` or `/video/av…` URL.
2. **Add resources → Import a collection:** name it and paste a
   `https://space.bilibili.com/USER/lists/ID?type=series` or `?type=season` URL.
   A discovery job resolves all pages and adds records to the collection. Repeat
   an import to discover additions. A resource can belong to multiple collections.
3. Select records and choose **Download**, **Parse**, **Export**, or **Full pipeline**.
   Parse requires downloaded audio; export requires saved transcript data. Full
   pipeline supplies those prerequisites automatically.
4. Enable **Dry run** in the job dialog to see the planned steps and prerequisite
   problems without network requests, media downloads, model loading, or resource
   changes. Discovery dry runs validate the URL and show the discovery plan; they
   do not enumerate remote videos. Dry-run jobs themselves are saved in history.
5. Open **Job queue** for per-resource progress, errors, activity, settings snapshots,
   and worker logs. **Pause** finishes the current resource before pausing.
   **Cancel** terminates the active child process. **Resume** continues paused work;
   **Retry incomplete** retries failed/cancelled items and preserves completed ones.
6. Click a resource title to listen to audio, preview transcript files, and download
   TXT, VTT, SRT, or JSON output. Use **Configuration** to change downloader,
   parser, and export defaults for future jobs.

Settings are captured when jobs are queued. Existing audio/transcripts are reused
unless **Reprocess existing results** is selected. To apply a new parser model to
an existing transcript, queue Parse with reprocessing enabled. Export always writes
a new version, so output changes never require rerunning Whisper.

### Video metadata before download

New link and collection imports automatically queue a **视频信息** job that
reads Bilibili's JSON metadata API (`/x/web-interface/view`). It records the
video title, total duration in seconds, part count, and each part's number,
title, CID and duration. No audio or video is downloaded by this job.

Each library entry displays its part count and total length; open the resource
to see per-part durations and links. For example, `BV1QcLTzfEj8` reports
**2 parts · 4:14:03**: P1 **2:00:30**, P2 **2:13:33**. Total duration is summed
across all parts, not taken from the first part. These are Bilibili's reported
video durations, not measurements of downloaded or trimmed audio.

For existing resources, select entries and click **视频信息**, or open a resource
and click **刷新视频信息**. This refreshes metadata without downloading or
changing transcripts/exports. Older libraries are migrated automatically;
unknown duration is displayed as unknown, never zero. Failed lookups remain
retryable in the job queue and preserve the last successful metadata. Reimport
queues missing metadata but avoids duplicating an already queued lookup.

The standalone scraper exposes `get_video_metadata(url)` and
`AudioScraper.resolve_metadata(url)` for the same metadata-only operation.

### Cloud transcription in the console

In **配置管理 → 解析器**, select **ASR Flash（阿里云 API）** or
**Paraformer v2（阿里云 API）**, choose the region, enter your Alibaba Cloud
Model Studio (DashScope/百炼) API key, and click **保存配置**.
Both parsers can use the same key within a region. Paraformer requires Beijing;
ASR Flash also supports Singapore, which requires a key issued in that region.
Get a key from [Alibaba Cloud's instructions](https://help.aliyun.com/zh/model-studio/get-api-key).

ASR Flash offers `qwen-audio-3.1-asr-flash-filetrans` (default) and
`qwen3-asr-flash-filetrans`. These are the long-recording APIs with subtitle
timestamps, rather than the short-audio chat APIs. Paraformer uses `paraformer-v2`.
Use `zh` for Chinese or `auto` for mixed languages. Whisper device, precision,
beam size and VAD settings apply only when the local parser is selected.

Select downloaded resources and run **解析音频** or **完整流程**. Uploading,
cloud task submission, polling, and result download happen automatically; you
do not need to create a storage bucket. Existing TXT, VTT, SRT and JSON exports
work with either parser. To replace an existing transcript after changing models,
run **Parse with Reprocess existing results** enabled. Running Export alone
does not transcribe or call a cloud model. Download and export remain independent.

Cloud recognition sends the audio to Alibaba and is billed by the provider.
Each file is limited to 12 hours / 2 GB (the temporary-upload policy can impose
a smaller size limit). Oversized files must be split before using this version.
Temporary uploads expire after 48 hours; this upload mode is intended for this
personal, sequential console rather than a high-concurrency production service.
See the [upload contract](https://help.aliyun.com/zh/model-studio/get-temporary-file-url)
and [file transcription contract](https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide).

Keys are stored in the local SQLite database separately from settings and are
never returned to the browser, job snapshots, or exported transcripts. Storage
is not encrypted: protect the data directory and its backups. Leave the key
field blank to retain it; use the delete checkbox to remove a saved key for the
selected region. Alternatively, set `DASHSCOPE_API_KEY` (Beijing) or
`DASHSCOPE_INTL_API_KEY` (Singapore) in the server environment. Saved keys take
precedence; deleting a saved key still allows an environment key to be used.
Jobs snapshot model/region/settings, but resolve the current key when they run,
so correcting a key also fixes retries of existing jobs.

Remote task IDs and completed part results are saved under `cloud-tasks/`.
**Retry incomplete** or resuming after a restart reuses those checkpoints.
Cancelling the local job stops waiting; it does not cancel Alibaba's task or
reverse its charges. The default wait limit is two hours and can be adjusted
in Configuration. Once a task ID has been saved, a network interruption does
not require another submission. If the submission response itself is lost,
the task ID cannot be recovered automatically; a retry may create another paid
task. Provider task/results expire, so resume interrupted jobs promptly.

After updating the application, rebuild Docker with `docker compose up -d --build`,
or run `uv sync --extra dev` and restart the local server. Refresh the browser
to load the new configuration panel. The legacy command-line tool continues
to use its own local Whisper settings; cloud selection is in the web console.

### Local development

```bash
uv sync --extra dev
uv run transcript-web
# Open http://localhost:8000

npm run check
uv run pytest -q tests audio-scraper/tests
```

FFmpeg must be on PATH for local audio conversion. The browser UI uses plain
HTML/CSS/JavaScript and is served by FastAPI; npm is only needed for the optional
JavaScript syntax check. Python dependencies are locked in `uv.lock`.

Environment variables: `TRANSCRIPT_DATA_DIR` (default `data/control-center`),
`HOST` (default `127.0.0.1`), `PORT` (default `8000`), and `HF_HOME` for model cache
location. Run **one application instance/worker per data directory**. API reference:
http://localhost:8000/docs (OpenAPI schema: `/openapi.json`).

### Persistence and recovery

Compose creates named volumes `studio-data` and `whisper-models` (prefixed by the
Compose project name). The data volume contains:

```text
/data/
  library.sqlite3          # collections, memberships, settings, jobs, item checkpoints
  audio/<resource>/job-N/  # persistent audio parts
  parsed/<resource>/      # canonical transcript JSON, independent of export formats
  exports/<resource>/job-N/# versioned readable output files
  logs/                   # per-item subprocess logs
```

Stop the service before backing up the complete data volume, and restore it at
the same `/data` path. Do not use `docker compose down -v` unless you intend to
delete the library and model cache. Old artifact versions and logs remain on disk;
this small personal tool does not automatically prune them.

After a restart, interrupted jobs become **paused** and retain completed items.
Explicitly resume them in the UI. A killed transcription restarts the current
resource, rather than resuming within a speech segment. Partial writes are not
registered as completed artifacts. New downloads and exports use versioned paths;
failed reprocessing leaves the last registered results available. Newly queued
jobs continue automatically.

Architecture: `web/store.py` owns persistence, `web/worker.py` owns scheduling and
process lifecycle, `web/stages.py` adapts discovery/download/parser operations,
`web/output.py` renders saved transcript data, and `web/app.py` exposes the API and
UI. One sequential worker keeps model memory use predictable. A failed resource
does not stop the remaining resources in a batch. Progress is polled every two
seconds; model initialization has an explanatory status rather than an invented
percentage. A failed collection import does not register a partial collection.

Bilibili access restrictions/rate limits are reported as job errors. This version
does not include cookie login or automatic import of legacy CLI output folders.
The existing CLI and its data remain independent.

## Legacy command-line tools

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

### Standalone audio scraper

The scraper is an independent `uv` project in `audio-scraper/`. It only installs
`requests` and `yt-dlp`, so collection discovery and downloading can run without
the transcription dependencies. Audio is saved as `.m4a` under `data/audio/` by
video ID. Install `ffmpeg` and ensure it is on your `PATH` for audio extraction.

```bash
# Preview the video URLs in a collection (no audio is downloaded)
uv run --project audio-scraper bilibili-audio-scraper list "https://space.bilibili.com/45502/lists/31284?type=series"

# Save the URL list for later use
uv run --project audio-scraper bilibili-audio-scraper list "https://space.bilibili.com/45502/lists/31284?type=series" --output videos.txt

# Download from a saved list, repeated video URLs, or directly from a collection
uv run --project audio-scraper bilibili-audio-scraper download --urls-file videos.txt
uv run --project audio-scraper bilibili-audio-scraper download --video "https://www.bilibili.com/video/BV1xx411c7mD" --video "https://www.bilibili.com/video/BV1nWpz6zE4U"
uv run --project audio-scraper bilibili-audio-scraper download --collection "https://space.bilibili.com/45502/lists/31284?type=series" --output-dir data/audio
```

The download command continues past individual failures and exits with a nonzero
status if any URL failed. Rerunning it skips existing Bilibili `.m4a` files;
`--force` downloads them again. Use `--timeout` and `--retries` to adjust network
behavior. URL files contain one URL per line, and may have
blank lines or lines starting with `#`.

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

The transcription command keeps downloaded audio in `data/transcripts/.audio_cache/`
so a failed transcription can reuse it on the next run.

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
