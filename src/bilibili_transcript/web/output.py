"""Render saved transcript data independently of download and speech recognition."""

import json
from pathlib import Path


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def timestamp(seconds, separator="."):
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{milliseconds:03}"


def export_transcript(document, directory, stem, settings):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    segments = document["segments"]
    for fmt in dict.fromkeys(settings["formats"]):
        if fmt == "json":
            content = json.dumps(document, ensure_ascii=False, indent=2)
        elif fmt == "txt":
            content = "\n".join(
                (
                    f"[{timestamp(s['start'])} → {timestamp(s['end'])}] "
                    if settings["timestamps"]
                    else ""
                )
                + s["text"]
                for s in segments
            )
        else:
            cues = []
            for index, segment in enumerate(segments, 1):
                separator = "," if fmt == "srt" else "."
                timing = f"{timestamp(segment['start'], separator)} --> {timestamp(segment['end'], separator)}"
                cues.append(
                    (f"{index}\n" if fmt == "srt" else "") + timing + "\n" + segment["text"]
                )
            content = ("WEBVTT\n\n" if fmt == "vtt" else "") + "\n\n".join(cues) + "\n"
        path = directory / f"{stem}.{fmt}"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        files.append(str(path))
    return files
