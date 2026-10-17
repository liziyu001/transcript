"""Studio integration contracts against an in-memory cloud, with no paid API calls."""

import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_pipeline import RAW, MemoryRecords

from transcript_cloud.api import create_app as cloud_app
from transcript_cloud.catalog import Catalog, ImportBatch, ResourceEdit, identity, video_url
from transcript_cloud.local import create_app as local_app
from transcript_cloud.local_stage import execute
from transcript_cloud.local_store import LocalStore
from transcript_cloud.local_worker import Worker
from transcript_cloud.models import (
    AudioRegistration,
    NewJob,
    OutputOptions,
    Settings,
    StudioSettings,
)
from transcript_cloud.service import Service

URL = "https://www.bilibili.com/video/BV1test123"
COLLECTION = "https://space.bilibili.com/123/lists/456?type=series"
TOKEN = "x" * 32


class PagedRecords(MemoryRecords):
    def list(self, kind, cursor=None, limit=100):
        rows = sorted(super().list(kind)["items"], key=lambda r: r["id"])
        if cursor:
            rows = [r for r in rows if r["id"] >= cursor]
        return {"items": rows[:limit], "cursor": rows[limit]["id"] if len(rows) > limit else None}


class InProcessCloud:
    def __init__(self, service):
        self.client = TestClient(cloud_app(service=service, settings=service.settings))

    def request(self, method, path, body=None):
        response = self.client.request(
            method, "/api" + path, json=body, headers={"Authorization": "Bearer " + TOKEN}
        )
        assert response.is_success, response.text
        return response.json()

    def all(self, kind):
        result, cursor = [], None
        while True:
            page = self.request("GET", f"/{kind}" + (f"?cursor={cursor}" if cursor else ""))
            result.extend(page["items"])
            cursor = page["cursor"]
            if not cursor:
                return result


@pytest.fixture
def system():
    settings = Settings(bucket="test", ots_instance="test", flow="test", panel_token=TOKEN)
    artifacts, flows, provider = Mock(), Mock(), Mock()
    artifacts.get_json.return_value = RAW
    artifacts.url.return_value = "https://test.oss-cn-beijing.aliyuncs.com/signed"
    provider.submit.return_value = "task-test"
    provider.check.return_value = {"task_status": "SUCCEEDED"}
    provider.fetch.return_value = RAW
    service = Service(PagedRecords(), artifacts, flows, provider, settings)
    return service, Catalog(service), InProcessCloud(service)


def register(service, digit="a", url=URL):
    return service.register(
        AudioRegistration(
            sha256=digit * 64,
            key=f"audio/{digit * 64}/source.m4a",
            size=10,
            title="测试音频",
            source_url=url,
        )
    )


def test_cloud_catalog_all_pages_reimport_and_membership(system):
    service, catalog, cloud = system
    for page in range(3):
        catalog.import_batch(
            ImportBatch(
                entries=[
                    {
                        "url": f"https://www.bilibili.com/video/BV{page}{n:03}",
                        "title": f"视频 {page}-{n}",
                        "metadata": {"duration": 120},
                    }
                    for n in range(100)
                ],
                collection_url=COLLECTION,
                name="合集",
            )
        )
    snapshot = cloud.request("GET", "/studio/library")
    assert len(snapshot["resources"]) == 300
    assert next(c for c in snapshot["collections"] if c["url"])["count"] == 300
    row = snapshot["resources"][0]
    catalog.edit(row["id"], ResourceEdit(metadata={"duration": 200, "pages": [{"page": 1}]}))
    result = catalog.import_batch(
        ImportBatch(
            entries=[{"url": row["url"], "metadata": {"duration": None}}],
            collection_url=COLLECTION,
            name="合集",
        )
    )
    assert result["existing"] == 1
    saved = catalog.resource(row["id"])
    assert saved["metadata"]["duration"] == 200 and saved["metadata"]["pages"]
    catalog.collection_edit(identity(COLLECTION), "新名字")
    catalog.collection_edit(identity(COLLECTION))
    assert catalog.resource(row["id"])["collections"] == ["general"]
    assert len(catalog.snapshot()["resources"]) == 300


def test_legacy_uploads_and_force_replacement_do_not_resurrect_old_audio(system):
    service, catalog, _ = system
    first = register(service)
    assert catalog.snapshot()["resources"][0]["audio"][0]["id"] == first["id"]
    second = register(service, "b")
    rid = identity(URL)
    catalog.edit(rid, ResourceEdit(audio_ids=[second["id"]]))
    assert [a["id"] for a in catalog.snapshot()["resources"][0]["audio"]] == [second["id"]]
    catalog.archive(rid)
    assert not catalog.snapshot()["resources"]
    catalog.import_batch(ImportBatch(entries=[{"url": URL}]))
    assert len(catalog.snapshot()["resources"]) == 1
    assert catalog.resource(rid)["audio_ids"] == [second["id"]]


def test_cloud_routes_auth_validation_and_signed_download(system):
    service, catalog, cloud = system
    assert cloud.client.get("/api/studio/library").status_code == 401
    catalog.import_batch(ImportBatch(entries=[{"url": URL}]))
    with pytest.raises(ValueError):
        catalog.edit(identity(URL), ResourceEdit(audio_ids=["missing"]))
    audio = register(service)
    catalog.edit(identity(URL), ResourceEdit(audio_ids=[audio["id"]]))
    response = cloud.request(
        "GET", f"/studio/resources/{identity(URL)}/files/audio/0?download=true"
    )
    assert response["url"].startswith("https://")
    service.artifacts.url.assert_called_with(audio["key"], expires=900, download=True)
    with pytest.raises(ValueError):
        video_url("https://www.bilibili.com.evil.test/video/BV123")


def test_legacy_request_identity_and_output_settings(system):
    service, _, _ = system
    audio = register(service)
    request = NewJob(audio_id=audio["id"], request_id="old-request")
    legacy_payload = request.model_dump(exclude={"output"})
    expected = hashlib.sha256(json.dumps(legacy_payload, sort_keys=True).encode()).hexdigest()[:32]
    assert service.create(request)["id"] == expected
    custom = service.create(
        NewJob(
            audio_id=audio["id"],
            request_id="new-request",
            output=OutputOptions(formats=["txt"], timestamps=True),
        )
    )
    service.step(custom["id"])
    service.step(custom["id"])
    assert set(service.require(custom["id"])["outputs"]) == {"txt"}
    assert "[00:00:01.234]" in service.artifacts.put.call_args.args[1]


def test_local_pipeline_uploads_all_parts_then_dispatches_and_replays(system, tmp_path):
    service, catalog, cloud = system
    catalog.import_batch(ImportBatch(entries=[{"url": URL}]))
    store = LocalStore(tmp_path)
    job = store.create("pipeline", [{"id": identity(URL)}], StudioSettings().model_dump())
    sources = [SimpleNamespace(path=tmp_path / f"p{n}.m4a", title=f"P{n}", url=URL) for n in (1, 2)]
    for source in sources:
        source.path.write_bytes(b"audio")
    calls = []

    def uploader(path, bucket, state_dir, title, url):
        digit = "a" if title == "P1" else "b"
        calls.append(title)
        return path, {
            "audio": {
                "sha256": digit * 64,
                "key": f"audio/{digit * 64}/source.m4a",
                "size": 5,
                "title": title,
                "source_url": url,
            }
        }

    result = execute(
        store, job, job["items"][0], cloud, download=lambda url: sources, uploader=uploader
    )
    assert len(result["remote_ids"]) == 2 and calls == ["P1", "P2"]
    assert len(catalog.resource(identity(URL))["audio_ids"]) == 2
    again = execute(
        store,
        job,
        job["items"][0],
        cloud,
        download=Mock(side_effect=AssertionError),
        uploader=Mock(side_effect=AssertionError),
    )
    assert result == again
    assert len(cloud.all("jobs")) == 2
    for jid in result["remote_ids"]:
        service.step(jid)
        service.step(jid)
    resource = catalog.snapshot()["resources"][0]
    assert len(resource["audio"]) == len(resource["parsed"]) == 2
    assert len(resource["outputs"]) == 8
    assert service.provider.submit.call_count == 2


def test_lost_submit_response_reuses_identity(system, tmp_path):
    service, catalog, cloud = system
    register(service)
    store = LocalStore(tmp_path)
    job = store.create("parse", [{"id": identity(URL)}], StudioSettings().model_dump(), force=True)
    original = cloud.request

    def lose_response(method, path, body=None):
        value = original(method, path, body)
        if path == "/jobs" and method == "POST":
            raise ConnectionError("response lost")
        return value

    cloud.request = lose_response
    with pytest.raises(ConnectionError):
        execute(store, job, job["items"][0], cloud)
    cloud.request = original
    result = execute(store, job, job["items"][0], cloud)
    assert len(result["remote_ids"]) == len(cloud.all("jobs")) == 1


def test_export_lost_response_does_not_create_another_revision(system, tmp_path):
    service, _, cloud = system
    audio = register(service)
    remote = service.create(NewJob(audio_id=audio["id"], request_id="parse-once"))
    service.step(remote["id"])
    service.step(remote["id"])
    store = LocalStore(tmp_path)
    job = store.create("export", [{"id": identity(URL)}], StudioSettings().model_dump())
    original = cloud.request

    def lose_response(method, path, body=None):
        result = original(method, path, body)
        if path.endswith("/export"):
            raise ConnectionError("response lost")
        return result

    cloud.request = lose_response
    with pytest.raises(ConnectionError):
        execute(store, job, job["items"][0], cloud)
    cloud.request = original
    result = execute(store, job, job["items"][0], cloud)
    assert result["remote_ids"] == [remote["id"]]
    assert service.require(remote["id"])["attempt"] == 2
    assert service.provider.submit.call_count == 1


def test_local_restart_pause_cancel_and_cloud_completion(system, tmp_path):
    service, _, cloud = system
    audio = register(service)
    remote = service.create(NewJob(audio_id=audio["id"], request_id="parse-once"))
    store = LocalStore(tmp_path)
    job = store.create("pipeline", [{"id": identity(URL)}], StudioSettings().model_dump())

    def waiting(j):
        j["state"] = "running"
        j["items"][0].update(state="waiting", result={"remote_ids": [remote["id"]]})

    store.change(job["id"], waiting)
    store.recover()
    assert store.job(job["id"])["state"] == "paused"
    service.step(remote["id"])
    service.step(remote["id"])
    Worker(store, cloud).poll_cloud()
    assert store.job(job["id"])["state"] == "completed"
    other = store.create("download", [{"id": identity(URL)}], StudioSettings().model_dump())
    store.control(other["id"], "cancel")
    assert store.job(other["id"])["items"][0]["state"] == "cancelled"
    store.control(other["id"], "retry")
    assert store.job(other["id"])["items"][0]["state"] == "pending"


def test_local_api_chinese_assets_cloud_metadata_and_origin_guard(system, tmp_path):
    _, catalog, cloud = system
    catalog.import_batch(
        ImportBatch(
            entries=[{"url": URL, "title": "中文课程"}], collection_url=COLLECTION, name="学习合集"
        )
    )
    app = local_app(tmp_path, cloud=cloud, start_worker=False)
    with TestClient(app, base_url="http://localhost") as client:
        assert "云端转录工作台" in client.get("/").text
        assert "Whisper 模型" not in client.get("/app.js").text
        library = client.get("/api/library").json()
        assert library["resources"][0]["title"] == "中文课程"
        assert TOKEN not in client.get("/api/settings").text
        response = client.post(
            "/api/jobs", json={"kind": "download", "resource_ids": [identity(URL)]}
        )
        assert response.status_code == 200
        jid = response.json()["id"]
        assert client.get(f"/api/jobs/{jid}").json()["config"]["parser"]["model"]
        assert (
            client.post(
                f"/api/jobs/{jid}/pause", headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert client.get("/api/library", headers={"Host": "evil.test"}).status_code == 403
        assert client.post(f"/api/jobs/{jid}/pause").json()["state"] == "paused"


def test_dry_run_never_invokes_scraper_uploader_or_cloud(system, tmp_path):
    _, _, _ = system
    store = LocalStore(tmp_path)
    config = StudioSettings().model_dump()
    config["collection"] = {"url": COLLECTION, "name": "test"}
    job = store.create("discover", [{}], config, dry_run=True)
    cloud = Mock()
    assert execute(store, job, job["items"][0], cloud)["steps"]
    cloud.request.assert_not_called()


def test_lower_concurrency_drains_old_slots(system):
    service, catalog, _ = system
    audio = register(service)
    jobs = [
        service.create(NewJob(audio_id=audio["id"], request_id=f"request-{n}")) for n in range(5)
    ]
    for j in jobs[:4]:
        service.step(j["id"])
    catalog.save_settings(StudioSettings(processing_concurrency=1))
    service.step(jobs[0]["id"])
    service.step(jobs[4]["id"])
    assert service.require(jobs[4]["id"])["state"] == "queued"
    for j in jobs[1:4]:
        service.step(j["id"])
    service.step(jobs[4]["id"])
    assert service.require(jobs[4]["id"])["state"] == "running"


def test_real_worker_process_dry_run_and_single_instance_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("TC_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("TC_PANEL_TOKEN", TOKEN)
    store = LocalStore(tmp_path)
    cloud = Mock()
    cloud.all.return_value = []
    worker = Worker(store, cloud)
    worker.start()
    try:
        with pytest.raises(RuntimeError, match="已有"):
            Worker(store, cloud).start()
        job = store.create(
            "parse", [{"id": "dry-resource"}], StudioSettings().model_dump(), dry_run=True
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and store.job(job["id"])["state"] != "completed":
            time.sleep(0.1)
        saved = store.job(job["id"])
        assert saved["state"] == "completed", saved
        assert "缺少前置条件" in saved["items"][0]["result"]["steps"][0]
        cloud.request.assert_not_called()
    finally:
        worker.close()


def test_failed_cloud_work_requires_explicit_recovery_or_reprocess(system, tmp_path):
    service, _, cloud = system
    audio = register(service)
    remote = service.create(NewJob(audio_id=audio["id"], request_id="failed-original"))
    remote.update(state="needs_attention")
    service.save(remote)
    store = LocalStore(tmp_path)
    job = store.create("parse", [{"id": identity(URL)}], StudioSettings().model_dump())
    with pytest.raises(ValueError, match="恢复"):
        execute(store, job, job["items"][0], cloud)
    assert len(cloud.all("jobs")) == 1


def test_completed_results_are_reused_until_force_selected(system, tmp_path):
    service, _, cloud = system
    audio = register(service)
    remote = service.create(NewJob(audio_id=audio["id"], request_id="completed-original"))
    service.step(remote["id"])
    service.step(remote["id"])
    store = LocalStore(tmp_path)
    settings = StudioSettings().model_dump()
    settings["parser"]["model"] = "paraformer-v2"
    job = store.create("parse", [{"id": identity(URL)}], settings)
    result = execute(store, job, job["items"][0], cloud)
    assert result["remote_ids"] == [remote["id"]]
    assert len(cloud.all("jobs")) == 1
