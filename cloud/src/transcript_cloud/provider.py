"""Short DashScope operations; waiting belongs to CloudFlow (Step Functions)."""

import math
import re

import httpx


class ProviderError(RuntimeError):
    pass


def normalize(document, title, source_url="", language="auto"):
    """Extracted from the local API parser; provider milliseconds become seconds."""
    tracks = document.get("transcripts")
    if not isinstance(tracks, list) or not tracks:
        raise ProviderError("Provider returned no transcript tracks")
    segments, detected = [], []
    for track in tracks:
        sentences = track.get("sentences")
        if not isinstance(sentences, list) or (track.get("text") and not sentences):
            raise ProviderError("Provider returned text without subtitle timestamps")
        for sentence in sentences:
            text = sentence.get("text", "").strip()
            if not text:
                continue
            try:
                start, end = (
                    float(sentence["begin_time"]) / 1000,
                    float(sentence["end_time"]) / 1000,
                )
                if not math.isfinite(start + end) or start < 0 or end < start:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise ProviderError("Invalid subtitle timestamps") from None
            segments.append({"start": start, "end": end, "text": text})
            if sentence.get("language"):
                detected.append(sentence["language"])
    if language == "auto":
        language = max(sorted(set(detected)), key=detected.count) if detected else "und"
    return {
        "schema_version": 1,
        "title": title,
        "url": source_url,
        "language": language,
        "segments": sorted(segments, key=lambda s: (s["start"], s["end"])),
    }


class DashScope:
    BASE = "https://dashscope.aliyuncs.com/api/v1"

    def __init__(self, key, client=None):
        if not key:
            raise ValueError("TC_DASHSCOPE_KEY is required for recognition")
        self.key = key
        self.client = client or httpx.Client(timeout=httpx.Timeout(45, connect=10))

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(
                method,
                self.BASE + path,
                headers={"Authorization": "Bearer " + self.key, "X-DashScope-Async": "enable"},
                **kwargs,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            # No automatic POST retry: a timeout may already have created a paid task.
            raise ProviderError("Model request failed; inspect/retry the saved job") from None

    def submit(self, url, options):
        model = options["model"]
        parameters = {"channel_id": [0]}
        if model == "qwen3-asr-flash-filetrans":
            source = {"file_url": url}
            parameters["enable_words"] = True
            if options["language"] != "auto":
                parameters["language"] = options["language"]
        else:
            source = {"file_urls": [url]}
            if options["language"] != "auto":
                parameters["language_hints"] = [options["language"]]
        result = self.request(
            "POST",
            "/services/audio/asr/transcription",
            json={
                "model": model,
                "input": source,
                "parameters": parameters,
            },
        )
        task_id = result.get("output", {}).get("task_id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", task_id):
            raise ProviderError("Submission response did not contain a task ID")
        return task_id

    def check(self, task_id):
        return self.request("GET", "/tasks/" + task_id)["output"]

    def fetch(self, output, model):
        from urllib.parse import urlparse

        result = (
            output.get("result", {})
            if model == "qwen3-asr-flash-filetrans"
            else (output.get("results") or [{}])[0]
        )
        if result.get("subtask_status") in ("FAILED", "CANCELED", "CANCELLED"):
            raise ProviderError("Recognition subtask failed")
        url = result.get("transcription_url", "")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".aliyuncs.com")
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ProviderError("Unexpected provider result URL")
        try:
            response = self.client.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderError("Could not retrieve provider result") from None
