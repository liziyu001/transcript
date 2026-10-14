# Bilibili Transcript

Transcribe Bilibili livestream recordings to text using speech recognition.

## Features

- Downloads audio-only from Bilibili video URLs (lightweight)
- Transcribes Chinese speech using Whisper
- Generates multiple output formats:
  - Plain text
  - Text with timestamps
  - JSON with metadata
  - WebVTT subtitles
- Runs locally (no cloud API required)
- Extensible architecture (swap scraper, parser, or models)

## Quick Start

### Setup

```bash
# Install uv if not already installed
# https://docs.astral.sh/uv/

# Sync dependencies
uv sync

# Activate virtual environment
.venv\Scripts\activate  # Windows
# or
source .venv/bin/activate  # macOS/Linux
```

### Usage

Edit `run.py` and set your video URL:

```python
VIDEO_URL = "https://www.bilibili.com/video/BVxxxxxx/"
```

Then run:

```bash
python run.py
```

Transcripts will be saved to `data/transcripts/`.

## Architecture

The codebase is organized for extensibility:

```
src/bilibili_transcript/
├── scraper.py        # Audio download (yt-dlp wrapper)
├── parser.py         # Transcription (Whisper wrapper)
├── orchestrator.py   # Coordination & I/O
└── main.py          # CLI entry point
```

### Components

- **Scraper**: Handles video URL → audio file
  - Currently: `yt-dlp` for Bilibili
  - Future: Add other platforms, API-based sources
  
- **Parser**: Handles audio file → transcript
  - Currently: `faster-whisper` (local)
  - Future: Swap to cloud APIs (OpenAI, Google, etc.)
  
- **Orchestrator**: Wires components together
  - Manages workflow, error handling, I/O
  - Extensible for batch processing

## Configuration

Edit `run.py` to change:

- **Model size**: `tiny`, `base`, `small`, `medium`, `large`
  - Smaller = faster but less accurate
  - Larger = slower but more accurate
  
- **Device**: `cpu` or `cuda` (if GPU available)
  - GPU is ~5-10x faster

- **Output directory**: Where transcripts are saved

## Output Formats

Each video generates 4 files with the video title and timestamp:

1. `title_20261014_120000.txt` — Plain text only
2. `title_20261014_120000_with_ts.txt` — Text with [HH:MM:SS] timestamps
3. `title_20261014_120000.json` — Full metadata including segments
4. `title_20261014_120000.vtt` — WebVTT subtitle format

## Model Information

Whisper base model:
- ~75 MB download
- ~1 GB RAM required
- ~2-4x real-time on CPU
- Good Chinese accuracy

For better accuracy, use `medium` (~1.5 GB) or `large` (~3 GB).

## Extending

### Add a new scraper (e.g., YouTube)

Create a new class in `scraper.py`:

```python
class YouTubeScraper(AudioScraper):
    def download_audio(self, url: str) -> AudioSource:
        # Custom implementation
        pass
```

### Add a new parser (e.g., OpenAI API)

Create a new class in `parser.py`:

```python
class OpenAIParser(WhisperParser):
    def transcribe(self, audio_path: Path) -> Transcript:
        # Call OpenAI API
        pass
```

Then swap in `orchestrator.py`:

```python
self.parser = OpenAIParser(parser_config)
```

## Troubleshooting

**"yt-dlp: command not found"**
- Ensure uv sync completed successfully
- Try: `pip install yt-dlp` manually

**"Whisper model download fails"**
- Check internet connection
- Models cache in `~/.cache/huggingface/`

**"Out of memory"**
- Use smaller model: `tiny` or `base`
- Or use GPU: set `device="cuda"`

## Requirements

- Python 3.9+
- yt-dlp (audio download)
- faster-whisper (transcription)
- 1+ GB free disk space
- Stable internet (for Bilibili, model downloads)

## Future Enhancements

- [ ] Batch processing (process multiple videos)
- [ ] Queue system for rate limiting
- [ ] Database storage instead of files
- [ ] Web UI for management
- [ ] Cloud API support (OpenAI, Google, Assembly AI)
- [ ] Post-processing (punctuation, formatting)
- [ ] Translation to English/other languages
