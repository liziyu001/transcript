"""DashScope file transcription: temporary upload, async task, timed segments.

Uses the REST API because temporary oss:// uploads are supported there for
both Qwen file transcription and Paraformer. No Whisper model is loaded.
"""

import json
import math
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from .parser import Transcript, TranscriptSegment

BASE_URLS = {
    "beijing": "https://dashscope.aliyuncs.com/api/v1",
    "singapore": "https://dashscope-intl.aliyuncs.com/api/v1",
}
ASR_MODELS = ("qwen-audio-3.1-asr-flash-filetrans", "qwen3-asr-flash-filetrans")


def parse_result(document, title, language="auto"):
    """Convert provider milliseconds into the existing export contract (seconds)."""
    tracks = document.get("transcripts")
    if not isinstance(tracks, list) or not tracks:
        raise RuntimeError("Cloud transcription returned no transcript tracks.")
    segments = []
    detected = []
    for track in tracks:
        sentences = track.get("sentences")
        if not isinstance(sentences, list) or (track.get("text") and not sentences):
            raise RuntimeError("Cloud transcription returned text without subtitle timestamps.")
        for sentence in sentences:
            text = sentence.get("text", "").strip()
            if not text:
                continue
            try:
                start = float(sentence["begin_time"]) / 1000
                end = float(sentence["end_time"]) / 1000
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise RuntimeError(
                    "Cloud transcription returned invalid subtitle timestamps."
                ) from None
            segments.append(TranscriptSegment(start, end, text))
            if sentence.get("language"):
                detected.append(sentence["language"])
    segments.sort(key=lambda s: (s.start, s.end))
    if language == "auto":
        language = max(set(detected), key=detected.count) if detected else "und"
    return Transcript(title=title, url="", language=language, segments=segments)


class DashScopeParser:
    def __init__(self, model, api_key, region="beijing", language="zh", timeout=7200):
        if model not in (*ASR_MODELS, "paraformer-v2"):
            raise ValueError("Unsupported cloud transcription model.")
        if region not in BASE_URLS or (model == "paraformer-v2" and region != "beijing"):
            raise ValueError("Paraformer requires the Beijing region and a Beijing API key.")
        if not api_key or not api_key.strip():
            raise ValueError("Configure an Alibaba Cloud API key in Configuration → Parser first.")
        self.model = model
        self.api_key = api_key.strip()
        self.region = region
        self.language = language or "auto"
        self.timeout = timeout
        self.base = BASE_URLS[region]

    def _error(self, data, fallback):
        # Provider errors can echo URLs or credentials; never persist those to logs.
        message = str(data.get("message") or data.get("code") or fallback)
        message = message.replace(self.api_key, "[redacted]")
        message = re.sub(r"(?:https?|oss)://\S+|\bsk-[A-Za-z0-9_-]+", "[redacted]", message)
        return message[:500]

    def _request(self, client, method, url, *, authenticated=False, **kwargs):
        headers = kwargs.pop("headers", {})
        if authenticated:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # Only reads are retried automatically; submitting twice can bill twice.
        attempts = 4 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = client.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError:
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(
                    "Cloud request failed or timed out. Check your network and retry the job."
                ) from None
            if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(2**attempt)
                continue
            if response.is_error:
                if response.status_code == 404 and authenticated and "/tasks/" in url:
                    raise RuntimeError(
                        "Cloud task is no longer available. Start a new Parse job with Reprocess existing results."
                    )
                if response.status_code in (401, 403) and authenticated:
                    raise RuntimeError(
                        "Cloud access denied. Check the API key, selected region and model access."
                    )
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                raise RuntimeError(
                    f"Cloud request failed (HTTP {response.status_code}): "
                    + self._error(data, "Check provider service status and retry.")
                )
            return response

    @staticmethod
    def _json(response):
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ValueError:
            raise RuntimeError("Cloud service returned an invalid JSON response.") from None

    @staticmethod
    def _storage_url(url):
        # Only provider storage, never a URL supplied by an arbitrary transcript.
        parsed = urlparse(url)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or not parsed.hostname.endswith(".aliyuncs.com")
            or parsed.username
            or parsed.port not in (None, 80, 443)
        ):
            raise RuntimeError("Cloud service returned an unexpected storage URL.")
        # Official result examples sometimes use HTTP; OSS supports HTTPS.
        return parsed._replace(scheme="https").geturl()

    def _upload(self, client, path, status):
        status("Getting cloud upload permission…")
        policy = self._json(
            self._request(
                client,
                "GET",
                self.base + "/uploads",
                authenticated=True,
                params={"action": "getPolicy", "model": self.model},
            )
        ).get("data", {})
        required = (
            "upload_host",
            "upload_dir",
            "oss_access_key_id",
            "signature",
            "policy",
            "x_oss_object_acl",
            "x_oss_forbid_overwrite",
        )
        if not all(key in policy for key in required):
            raise RuntimeError("Cloud service did not return a usable upload permission.")
        limit = float(policy.get("max_file_size_mb", 2048)) * 1024 * 1024
        if path.stat().st_size > limit:
            raise ValueError(
                "Audio exceeds the cloud upload size limit. Split the recording first."
            )
        name = uuid.uuid4().hex + path.suffix.lower()
        key = policy["upload_dir"].rstrip("/") + "/" + name
        data = {
            "OSSAccessKeyId": policy["oss_access_key_id"],
            "Signature": policy["signature"],
            "policy": policy["policy"],
            "x-oss-object-acl": policy["x_oss_object_acl"],
            "x-oss-forbid-overwrite": str(policy["x_oss_forbid_overwrite"]).lower(),
            "key": key,
            "success_action_status": "200",
        }
        status("Uploading audio to Alibaba Cloud…")
        # httpx streams the open file; multi-hour recordings are not read into RAM.
        with path.open("rb") as audio:
            self._request(
                client,
                "POST",
                self._storage_url(policy["upload_host"]),
                data=data,
                files={"file": (name, audio, "application/octet-stream")},
                timeout=httpx.Timeout(300, connect=30),
            )
        return "oss://" + key

    def _body(self, file_url):
        parameters = {"channel_id": [0]}
        if self.model == "qwen3-asr-flash-filetrans":
            source = {"file_url": file_url}
            parameters["enable_words"] = True
            if self.language != "auto":
                parameters["language"] = self.language
        else:
            source = {"file_urls": [file_url]}
            if self.language != "auto":
                parameters["language_hints"] = [self.language]
        return {"model": self.model, "input": source, "parameters": parameters}

    def transcribe(self, audio_path, progress=None, *, checkpoint=None, status=None):
        path = Path(audio_path)
        status = status or (lambda message: print(message, flush=True))
        if not path.is_file():
            raise FileNotFoundError("Downloaded audio is missing. Download this resource again.")
        stat = path.stat()
        if not 0 < stat.st_size <= 2 * 1024**3:
            raise ValueError("Cloud transcription requires a nonempty audio file up to 2 GB.")
        identity = {
            "path": str(path.resolve()),
            "size": stat.st_size,
            "mtime": stat.st_mtime_ns,
            "model": self.model,
            "region": self.region,
            "language": self.language,
        }
        saved = {}
        checkpoint = Path(checkpoint) if checkpoint else None
        if checkpoint and checkpoint.exists():
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if saved.get("identity") != identity:
                saved = {}
        saved["identity"] = identity

        def save():
            if checkpoint:
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                temporary = checkpoint.with_suffix(".tmp")
                temporary.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
                temporary.replace(checkpoint)

        if "transcript" in saved:
            status("Reusing saved cloud transcript…")
            document = saved["transcript"]
            return Transcript(
                document["title"],
                document["url"],
                document["language"],
                [TranscriptSegment(**s) for s in document["segments"]],
            )
        with httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=False) as client:
            if not saved.get("task_id"):
                file_url = self._upload(client, path, status)
                status("Submitting cloud transcription…")
                response = self._json(
                    self._request(
                        client,
                        "POST",
                        self.base + "/services/audio/asr/transcription",
                        authenticated=True,
                        headers={
                            "X-DashScope-Async": "enable",
                            "X-DashScope-OssResourceResolve": "enable",
                        },
                        json=self._body(file_url),
                    )
                )
                task_id = response.get("output", {}).get("task_id")
                if not task_id:
                    raise RuntimeError(
                        self._error(response, "Cloud service did not return a task ID.")
                    )
                saved["task_id"] = task_id
                save()
            else:
                status("Resuming saved cloud transcription…")
            deadline = time.monotonic() + self.timeout
            while True:
                response = self._json(
                    self._request(
                        client,
                        "GET",
                        self.base + "/tasks/" + quote(saved["task_id"], safe=""),
                        authenticated=True,
                    )
                )
                output = response.get("output", {})
                task_status = output.get("task_status")
                if task_status == "SUCCEEDED":
                    break
                if task_status in ("FAILED", "CANCELED", "CANCELLED", "UNKNOWN"):
                    saved.pop("task_id", None)
                    save()  # Explicit user retry may now submit a replacement task.
                    raise RuntimeError(
                        "Cloud transcription failed: " + self._error(output, task_status)
                    )
                if task_status not in ("PENDING", "RUNNING"):
                    raise RuntimeError(
                        self._error(response, "Cloud service returned an unknown task status.")
                    )
                status(
                    "Cloud transcription queued…"
                    if task_status == "PENDING"
                    else "Cloud transcription running…"
                )
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Cloud wait timed out. Retry incomplete to continue the saved task."
                    )
                time.sleep(min(5, max(0, deadline - time.monotonic())))
            if self.model == "qwen3-asr-flash-filetrans":
                result = output.get("result", {})
            else:
                results = output.get("results", [])
                result = results[0] if results else {}
            if result.get("subtask_status") in ("FAILED", "CANCELED", "CANCELLED"):
                saved.pop("task_id", None)
                save()
                raise RuntimeError(
                    "Cloud transcription failed: " + self._error(result, "File recognition failed.")
                )
            if not result.get("transcription_url"):
                raise RuntimeError("Cloud transcription completed without a result file.")
            status("Downloading cloud transcript…")
            document = self._json(
                self._request(
                    client,
                    "GET",
                    self._storage_url(result["transcription_url"]),
                )
            )
        transcript = parse_result(document, path.stem, self.language)
        saved["transcript"] = asdict(transcript)
        save()
        status(f"Cloud transcription complete: {len(transcript.segments)} segments")
        if progress:
            duration = max((s.end for s in transcript.segments), default=1)
            progress(duration, duration)
        return transcript

    def unload_model(self):
        """Compatibility with the local parser lifecycle; no persistent model."""
