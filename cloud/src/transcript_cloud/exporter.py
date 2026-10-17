"""Pure exporters extracted from the local studio's output contract."""

import json


def timestamp(seconds, separator="."):
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{milliseconds:03}"


def render(document, timestamps=False):
    outputs = {
        "json": json.dumps(document, ensure_ascii=False, indent=2),
        "txt": "\n".join(
            (f"[{timestamp(s['start'])}] " if timestamps else "") + s["text"]
            for s in document["segments"]
        ),
    }
    for fmt in ("srt", "vtt"):
        cues = []
        for index, segment in enumerate(document["segments"], 1):
            sep = "," if fmt == "srt" else "."
            timing = f"{timestamp(segment['start'], sep)} --> {timestamp(segment['end'], sep)}"
            cues.append((f"{index}\n" if fmt == "srt" else "") + timing + "\n" + segment["text"])
        outputs[fmt] = ("WEBVTT\n\n" if fmt == "vtt" else "") + "\n\n".join(cues) + "\n"
    return outputs
