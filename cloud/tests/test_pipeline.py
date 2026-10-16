"""Focused offline recovery/contract tests. No live model calls or cloud deployment."""

import asyncio
import base64
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from transcript_cloud.api import create_app
from transcript_cloud.exporter import render
from transcript_cloud.handlers import invoke_asgi
from transcript_cloud.models import AudioRegistration, NewJob, Settings
from transcript_cloud.provider import DashScope, ProviderError, normalize
from transcript_cloud.service import Service
from transcript_cloud.storage import Conflict

SHA = "a" * 64
RAW = {
    "transcripts": [
        {
            "sentences": [
                {"begin_time": 1234, "end_time": 4567, "text": "你好，世界", "language": "zh"}
            ]
        }
    ]
}


class MemoryRecords:
    def __init__(self):
        self.data = {}

    def get(self, kind, key):
        return copy.deepcopy(self.data.get((kind, key)))

    def put(self, kind, key, value, revision=None):
        previous = self.data.get((kind, key))
        if (previous and previous["revision"] != revision) or (
            not previous and revision is not None
        ):
            raise Conflict("changed")
        new = copy.deepcopy({**value, "revision": (revision or 0) + 1, "id": key})
        self.data[kind, key] = new
        return copy.deepcopy(new)

    def list(self, kind, cursor=None, limit=100):
        return {
            "items": [copy.deepcopy(v) for (k, _), v in self.data.items() if k == kind],
            "cursor": None,
        }


@pytest.fixture
def service():
    settings = Settings(bucket="test", ots_instance="test", flow="test", panel_token="x" * 32)
    artifacts, flows, provider = Mock(), Mock(), Mock()
    artifacts.url.return_value = "https://bucket.oss-cn-beijing.aliyuncs.com/audio"
    artifacts.get_json.return_value = copy.deepcopy(RAW)
    provider.submit.return_value = "task-1"
    provider.check.return_value = {"task_status": "SUCCEEDED"}
    provider.fetch.return_value = copy.deepcopy(RAW)
    s = Service(MemoryRecords(), artifacts, flows, provider, settings, clock=lambda: 1000)
    s.register(
        AudioRegistration(
            sha256=SHA, key=f"audio/{SHA}/source.m4a", title="测试", size=100, duration=10
        )
    )
    return s


def new_job(service, request_id="request-1"):
    return service.create(NewJob(audio_id=SHA, request_id=request_id))


def test_replayed_request_and_worker_do_not_resubmit(service):
    job = new_job(service)
    assert new_job(service)["id"] == job["id"]
    assert not service.step(job["id"])["done"]
    assert service.require(job["id"])["task_id"] == "task-1"
    assert service.step(job["id"])["done"]
    assert service.step(job["id"])["done"]
    service.provider.submit.assert_called_once()
    assert set(service.require(job["id"])["outputs"]) == {"txt", "json", "vtt", "srt"}


def test_ambiguous_submission_requires_recovery(service):
    service.provider.submit.side_effect = TimeoutError("secret must not appear in records")
    job = new_job(service)
    service.step(job["id"])
    saved = service.require(job["id"])
    assert saved["state"] == "needs_attention"
    assert "secret" not in saved["error"]
    with pytest.raises(ValueError, match="unknown"):
        service.retry(job["id"])
    service.retry(job["id"], "recovered-task")
    service.step(job["id"])
    service.provider.submit.assert_called_once()
    service.provider.check.assert_called_once_with("recovered-task")


def test_process_death_after_submit_checkpoint(service):
    job = new_job(service)
    job.update(state="submitting", lease_until=900)
    service.save(job)
    assert service.step(job["id"])["done"]
    assert service.require(job["id"])["state"] == "needs_attention"
    service.provider.submit.assert_not_called()


def test_export_failure_preserves_paid_result(service):
    job = new_job(service)
    service.step(job["id"])
    service.artifacts.put.side_effect = OSError("disk")
    for _ in range(3):
        service.step(job["id"])
    assert service.require(job["id"])["raw_key"]
    assert service.require(job["id"])["state"] == "failed"
    service.artifacts.put.side_effect = None
    service.retry(job["id"])
    service.step(job["id"])
    service.provider.submit.assert_called_once()
    service.provider.check.assert_called_once()
    service.reexport(job["id"])
    service.step(job["id"])
    assert service.require(job["id"])["state"] == "completed"
    service.provider.submit.assert_called_once()


def test_active_lease_and_global_slots(service):
    service.settings.max_active = 1
    a, b = new_job(service, "request-a"), new_job(service, "request-b")
    service.step(a["id"])
    service.step(b["id"])
    assert service.require(b["id"])["state"] == "queued"
    service.provider.submit.assert_called_once()
    service.step(a["id"])
    service.step(b["id"])
    assert service.provider.submit.call_count == 2
    b = service.require(b["id"])
    b["lease_until"] = 1100
    service.save(b)
    assert not service.step(b["id"])["done"]
    with pytest.raises(Conflict):
        service.retry(b["id"])


def test_workflow_dispatch_failure_is_replayable(service):
    service.flows.start.side_effect = RuntimeError("network")
    with pytest.raises(RuntimeError):
        new_job(service)
    assert len(service.records.list("job")["items"]) == 1
    service.flows.start.side_effect = None
    job = new_job(service)
    assert len(service.records.list("job")["items"]) == 1
    service.step(job["id"])
    service.provider.submit.assert_called_once()


def test_timeout_keeps_remote_task_and_slot(service):
    job = new_job(service)
    service.step(job["id"])
    service.clock = lambda: 100000
    service.step(job["id"])
    assert service.require(job["id"])["task_id"] == "task-1"
    service.retry(job["id"])
    service.step(job["id"])
    service.provider.submit.assert_called_once()


@pytest.mark.parametrize(
    "model,key",
    [
        ("qwen3-asr-flash-filetrans", "file_url"),
        ("qwen-audio-3.1-asr-flash-filetrans", "file_urls"),
        ("paraformer-v2", "file_urls"),
    ],
)
def test_provider_shapes(model, key):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"output": {"task_id": "task-test"}})

    provider = DashScope("test-key", httpx.Client(transport=httpx.MockTransport(respond)))
    assert provider.submit("https://audio", {"model": model, "language": "zh"}) == "task-test"
    body = json.loads(requests[0].content)
    assert key in body["input"]
    assert "X-DashScope-OssResourceResolve" not in requests[0].headers


def test_subtitle_contract_and_invalid_times():
    doc = normalize(RAW, "test")
    assert doc["language"] == "zh"
    outputs = render(doc)
    assert "00:00:01,234 --> 00:00:04,567" in outputs["srt"]
    assert outputs["vtt"].startswith("WEBVTT")
    invalid = copy.deepcopy(RAW)
    invalid["transcripts"][0]["sentences"][0]["begin_time"] = float("nan")
    with pytest.raises(ProviderError):
        normalize(invalid, "test")


def test_api_auth_and_fc_http_bridge(service):
    app = create_app(service, service.settings)
    client = TestClient(app)
    assert client.get("/api/audio").status_code == 401
    headers = {"Authorization": "Bearer " + service.settings.panel_token}
    assert client.get("/api/audio", headers=headers).json()["items"][0]["title"] == "测试"
    response = client.options(
        "/api/audio",
        headers={
            "Origin": "http://localhost:8787",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 200
    event = {
        "requestContext": {"http": {"method": "GET", "path": "/api/audio"}},
        "headers": headers,
        "body": "",
        "isBase64Encoded": False,
    }
    response = asyncio.run(invoke_asgi(app, event))
    assert response["statusCode"] == 200
    assert json.loads(base64.b64decode(response["body"]))["items"][0]["id"] == SHA
    assert client.get("/").status_code == 200


def test_package_has_no_local_model_imports():
    root = Path(__file__).parents[1] / "src" / "transcript_cloud"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import faster_whisper" not in text
        assert "from bilibili_transcript" not in text
