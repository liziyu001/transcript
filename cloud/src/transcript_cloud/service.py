"""Durable state transitions. Nothing here requires a running local process."""

import hashlib
import json
import time
import uuid

from .exporter import render
from .provider import normalize
from .storage import Conflict

TERMINAL = {"completed", "failed", "needs_attention", "dismissed"}
LEASE_SECONDS = 300  # Longer than the deployed worker's 180-second invocation timeout.


class Service:
    def __init__(self, records, artifacts, flows, provider, settings, clock=time.time):
        self.records, self.artifacts, self.flows = records, artifacts, flows
        self.provider, self.settings, self.clock = provider, settings, clock

    def register(self, audio):
        existing = self.records.get("audio", audio.sha256)
        if existing and existing.get("state") == "deleting":
            raise Conflict("OSS 文件清理尚未完成，请先重试清理。")
        self.artifacts.verify(audio)
        if existing:
            if existing.get("state") == "deleted":
                return self.records.put(
                    "audio",
                    audio.sha256,
                    {
                        **existing,
                        **audio.model_dump(),
                        "state": "ready",
                        "deleted_at": None,
                    },
                    existing["revision"],
                )
            return existing
        try:
            return self.records.put(
                "audio",
                audio.sha256,
                {**audio.model_dump(), "created": self.clock(), "state": "ready"},
            )
        except Conflict:
            current = self.records.get("audio", audio.sha256)
            if not current or current.get("state") != "ready":
                raise
            return current

    def reserve_media(self, audio_id, job_id, attempt, require_audio=True):
        """Durable intent fences deletion before a new job/attempt becomes visible.

        A crashed request can replay its job identity. Cleanup refuses incomplete intents
        rather than guessing whether a provider request may still be in flight.
        """
        for _ in range(5):
            audio = self.records.get("audio", audio_id)
            if not audio or audio.get("state") == "deleting":
                raise Conflict("OSS 文件正在清理，请完成清理后再创建任务。")
            if require_audio and audio.get("state") == "deleted":
                raise ValueError("OSS 音频已删除，请先重新下载并上传。")
            guards = dict(audio.get("job_guards", {}))
            guards[job_id] = max(guards.get(job_id, 0), attempt)
            try:
                return self.records.put(
                    "audio", audio_id, {**audio, "job_guards": guards}, audio["revision"]
                )
            except Conflict:
                continue
        raise Conflict("音频记录已更改，请重试。")

    def create(self, request):
        audio = self.records.get("audio", request.audio_id)
        if not audio:
            raise ValueError("Audio is not registered")
        # Keep the pre-Studio request identity for default output settings so old CLI
        # checkpoints remain idempotent across deployment of the new API.
        from .models import OutputOptions

        payload = request.model_dump()
        if request.output == OutputOptions():
            payload.pop("output")
        identity = json.dumps(payload, sort_keys=True)
        job_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
        job = self.records.get("job", job_id)
        if not job:
            self.reserve_media(request.audio_id, job_id, 1)
            try:
                job = self.records.put(
                    "job",
                    job_id,
                    {
                        "audio_id": request.audio_id,
                        "title": audio["title"],
                        "options": request.options.model_dump(),
                        "output": request.output.model_dump(),
                        "state": "queued",
                        "attempt": 1,
                        "created": self.clock(),
                        "updated": self.clock(),
                        "lease_until": 0,
                        "deadline": self.clock() + self.settings.job_timeout,
                        "task_id": None,
                        "outputs": {},
                        "error": "",
                    },
                )
            except Conflict:
                job = self.records.get("job", job_id)
        # Start may fail after the record was persisted. Repeating this request resumes dispatch.
        if job["state"] not in TERMINAL:
            self.flows.start(job)
        return job

    def retry(self, job_id, task_id=None):
        job = self.require(job_id)
        if job.get("lease_until", 0) > self.clock():
            raise Conflict("Worker is still active; retry after its lease expires")
        if job["state"] == "needs_attention" and not (task_id or job.get("task_id")):
            raise ValueError(
                "Submission outcome is unknown. Attach its provider task ID, or create "
                "an explicitly new paid job from the library."
            )
        if job["state"] not in {"failed", "needs_attention"}:
            raise ValueError("Only failed or uncertain jobs can be retried")
        if job.get("results_deleted"):
            raise ValueError("转录结果已删除，请从资源库创建新的解析任务。")
        self.reserve_media(
            job["audio_id"], job_id, job["attempt"] + 1, require_audio=not bool(job.get("raw_key"))
        )
        if task_id:
            job["task_id"] = task_id
        job.update(
            state="exporting"
            if job.get("raw_key")
            else "running"
            if job.get("task_id")
            else "queued",
            error="",
            stage_failures=0,
            attempt=job["attempt"] + 1,
            deadline=self.clock() + self.settings.job_timeout,
            lease_until=0,
        )
        job = self.save(job)
        self.flows.start(job)
        return job

    def resume(self, job_id):
        job = self.require(job_id)
        if job["state"] in TERMINAL:
            raise ValueError("Use retry for a stopped job")
        if job.get("lease_until", 0) > self.clock():
            raise Conflict("Worker is active")
        self.reserve_media(
            job["audio_id"], job_id, job["attempt"] + 1, require_audio=not bool(job.get("raw_key"))
        )
        # Explicit recovery for a workflow that failed outside the worker.
        job["attempt"] += 1
        job = self.save(job)
        self.flows.start(job)
        return job

    def reexport(self, job_id, output=None):
        job = self.require(job_id)
        if job["state"] != "completed" or not job.get("raw_key"):
            raise ValueError("Only completed jobs with saved raw results can be re-exported")
        self.reserve_media(job["audio_id"], job_id, job["attempt"] + 1, require_audio=False)
        if output is not None:
            job["output"] = output
        job.update(
            state="exporting",
            attempt=job["attempt"] + 1,
            lease_until=0,
            deadline=self.clock() + self.settings.job_timeout,
        )
        job = self.save(job)
        self.flows.start(job)
        return job

    def dismiss(self, job_id):
        """User has checked the provider and explicitly releases an unresolved task's slot."""
        job = self.require(job_id)
        if job["state"] not in {"failed", "needs_attention"}:
            raise ValueError("Only failed or uncertain jobs can be dismissed")
        job.update(
            state="dismissed",
            remote_done=True,
            lease_until=0,
            error="Dismissed after manual provider check; no remote cancellation was sent",
        )
        return self.save(job)

    def require(self, job_id):
        job = self.records.get("job", job_id)
        if not job:
            raise ValueError("Job not found")
        return job

    def save(self, job):
        job["updated"] = self.clock()
        return self.records.put("job", job["id"], job, job["revision"])

    def step(self, job_id):
        job = self.require(job_id)
        if job["state"] in TERMINAL:
            return {"job_id": job_id, "done": True}
        if job.get("lease_until", 0) > self.clock():
            return {"job_id": job_id, "done": False}
        if job["state"] == "submitting":
            # Previous process may have died between provider acceptance and checkpoint.
            job.update(state="needs_attention", error="Submission interrupted; recover task ID")
            self.save(job)
            return {"job_id": job_id, "done": True}
        if self.clock() > job["deadline"]:
            job.update(state="failed", error="Job wait limit reached; retry to resume saved task")
            self.save(job)
            return {"job_id": job_id, "done": True}
        job.update(lease_until=self.clock() + LEASE_SECONDS, lease_owner=uuid.uuid4().hex)
        try:
            job = self.save(job)
        except Conflict:
            return {"job_id": job_id, "done": False}
        try:
            audio = self.records.get("audio", job["audio_id"])
            if job["state"] == "queued":
                # Generate URL before entering the ambiguous submission window.
                url = self.artifacts.url(audio["key"], expires=86400)
                job["state"] = "submitting"
                job = self.save(job)
                task_id = self.provider.submit(url, job["options"])
                job.update(task_id=task_id, state="running")
                job = self.save(job)
            elif job["state"] == "running":
                output = self.provider.check(job["task_id"])
                status = output.get("task_status")
                if status == "SUCCEEDED":
                    raw = self.provider.fetch(output, job["options"]["model"])
                    key = f"results/{job_id}/raw.json"
                    self.artifacts.put_json(key, raw)
                    job.update(raw_key=key, state="exporting", remote_done=True)
                    job = self.save(job)
                elif status in ("FAILED", "CANCELED", "CANCELLED", "UNKNOWN"):
                    job.update(
                        state="failed", remote_done=True, error="Provider task ended: " + status
                    )
                elif status not in ("PENDING", "RUNNING"):
                    raise ValueError("Unrecognized provider task state")
            if job["state"] == "exporting":
                raw = self.artifacts.get_json(job["raw_key"])
                document = normalize(
                    raw, audio["title"], audio["source_url"], job["options"]["language"]
                )
                document["parser"] = job["options"]
                outputs = {}
                prefix = f"results/{job_id}/exports/{job['attempt']}"
                output = job.get("output", {})
                for fmt, content in render(document, output.get("timestamps", False)).items():
                    if fmt not in output.get("formats", ["txt", "srt", "vtt", "json"]):
                        continue
                    key = f"{prefix}/transcript.{fmt}"
                    self.artifacts.put(
                        key,
                        content,
                        "application/json" if fmt == "json" else "text/plain; charset=utf-8",
                    )
                    outputs[fmt] = key
                job.update(outputs=outputs, state="completed", error="")
        except Conflict:
            # Never overwrite another invocation's state after a conditional-write failure.
            raise
        except Exception as exc:
            stage = job["state"]
            failures = job.get("stage_failures", 0) + 1
            # Polling and exports can be retried safely. Submission cannot.
            state = (
                "needs_attention"
                if stage == "submitting"
                else stage
                if stage in {"running", "exporting"} and failures < 3
                else "failed"
            )
            job.update(
                state=state,
                stage_failures=failures,
                error=(
                    "Submission outcome uncertain; recover provider task ID"
                    if stage == "submitting"
                    else f"{stage} failed ({type(exc).__name__}); "
                    + (
                        "retrying automatically"
                        if state != "failed"
                        else "retry saved work to resume"
                    )
                ),
            )
            # Do not log arbitrary SDK exception messages (may contain credentials/signed URLs).
            import logging
            import traceback

            logging.getLogger(__name__).error(
                "Job %s stage=%s error_type=%s task_saved=%s frames=%s",
                job_id,
                stage,
                type(exc).__name__,
                bool(job.get("task_id")),
                [
                    (frame.filename, frame.lineno, frame.name)
                    for frame in traceback.extract_tb(exc.__traceback__)
                ],
            )
        else:
            if job["state"] != "failed":
                job.update(stage_failures=0, error="")
        job["lease_until"] = 0
        self.save(job)
        return {"job_id": job_id, "done": job["state"] in TERMINAL}
