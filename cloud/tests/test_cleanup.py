from unittest.mock import Mock

import pytest
import test_studio
from test_studio import TOKEN, URL, register

from transcript_cloud.catalog import ImportBatch, ResourceEdit, identity
from transcript_cloud.cleanup import CleanupRequest, cleanup
from transcript_cloud.models import NewJob
from transcript_cloud.storage import Artifacts, Conflict


@pytest.fixture
def system():
    return test_studio.system.__wrapped__()


def completed(service):
    audio = register(service)
    job = service.create(NewJob(audio_id=audio["id"], request_id="cleanup-test"))
    service.step(job["id"])
    service.step(job["id"])
    return audio, service.require(job["id"])


def request(category="audio", ids=None):
    return CleanupRequest(resource_ids=ids or [identity(URL)], category=category, confirmed=True)


def test_delete_audio_preserves_results_and_reexport(system):
    service, catalog, _ = system
    audio, job = completed(service)
    cleanup(catalog, request())
    service.artifacts.delete.assert_called_once_with(audio["key"])
    row = catalog.snapshot()["resources"][0]
    assert row["audio"] == [] and row["parsed_complete"] and row["exported_complete"]
    assert row["audio_deleted_count"] == 1
    service.reexport(job["id"])
    service.step(job["id"])
    assert service.require(job["id"])["state"] == "completed"
    assert service.provider.submit.call_count == 1
    with pytest.raises(ValueError):
        service.create(NewJob(audio_id=audio["id"], request_id="new-parse"))
    register(service)
    assert catalog.snapshot()["resources"][0]["audio"]


@pytest.mark.parametrize("category", ["outputs", "results", "all"])
def test_cleanup_all_export_revisions(system, category):
    service, catalog, _ = system
    audio, job = completed(service)
    service.reexport(job["id"])
    service.step(job["id"])
    cleanup(catalog, request(category))
    keys = {c.args[0] for c in service.artifacts.delete.call_args_list}
    assert len([k for k in keys if "/exports/" in k]) == 8
    saved = service.require(job["id"])
    assert not saved["outputs"]
    assert bool(saved.get("raw_key")) == (category == "outputs")
    assert (audio["key"] in keys) == (category == "all")
    if category == "outputs":
        service.reexport(job["id"])
        service.step(job["id"])
        assert service.require(job["id"])["outputs"]


@pytest.mark.parametrize("state", ["queued", "running", "needs_attention", "exporting"])
def test_active_jobs_block_cleanup(system, state):
    service, catalog, _ = system
    _, job = completed(service)
    job["state"] = state
    service.save(job)
    with pytest.raises(Conflict):
        cleanup(catalog, request())
    service.artifacts.delete.assert_not_called()


def test_interrupted_cleanup_is_retryable_and_blocks_jobs(system):
    service, catalog, _ = system
    audio, job = completed(service)
    service.artifacts.delete.side_effect = RuntimeError("OSS unavailable")
    with pytest.raises(Conflict):
        cleanup(catalog, request())
    assert service.records.get("audio", audio["id"])["state"] == "deleting"
    with pytest.raises(Conflict):
        service.reexport(job["id"])
    with pytest.raises(Conflict):
        cleanup(catalog, request("all"))
    service.artifacts.delete.side_effect = None
    cleanup(catalog, request())
    assert service.records.get("audio", audio["id"])["state"] == "deleted"


def test_shared_audio_requires_all_resources(system):
    service, catalog, _ = system
    audio, _ = completed(service)
    catalog.edit(identity(URL), ResourceEdit(audio_ids=[audio["id"]]))
    other = "https://www.bilibili.com/video/BVother"
    catalog.import_batch(ImportBatch(entries=[{"url": other}]))
    catalog.edit(identity(other), ResourceEdit(audio_ids=[audio["id"]]))
    with pytest.raises(Conflict):
        cleanup(catalog, request())
    cleanup(catalog, request(ids=[identity(URL), identity(other)]))
    service.artifacts.delete.assert_called_once()


def test_pending_submission_and_versioned_bucket_block(system):
    service, catalog, _ = system
    audio, _ = completed(service)
    service.artifacts.ensure_unversioned.side_effect = ValueError("versioned bucket")
    with pytest.raises(ValueError):
        cleanup(catalog, request())
    assert service.records.get("audio", audio["id"])["state"] == "ready"
    service.artifacts.ensure_unversioned.side_effect = None
    service.reserve_media(audio["id"], "pending", 1)
    with pytest.raises(Conflict):
        cleanup(catalog, request())
    service.artifacts.delete.assert_not_called()


def test_api_confirmation_and_auth(system):
    service, _, cloud = system
    completed(service)
    body = {"resource_ids": [identity(URL)], "category": "audio"}
    assert cloud.client.post("/api/studio/cleanup", json=body).status_code == 401
    assert (
        cloud.client.post(
            "/api/studio/cleanup", json=body, headers={"Authorization": "Bearer " + TOKEN}
        ).status_code
        == 422
    )
    service.artifacts.delete.assert_not_called()
    assert (
        cloud.request("POST", "/studio/cleanup", {**body, "confirmed": True})["audio_bytes"] == 10
    )


def test_storage_delete_exact_paths_only():
    artifacts = Artifacts(Mock())
    bucket = Mock()
    artifacts.bucket = Mock(return_value=bucket)
    for key in ["audio/", "results/other", "code/function.zip", "../source.m4a"]:
        with pytest.raises(ValueError):
            artifacts.delete(key)
    bucket.delete_object.assert_not_called()
    key = "audio/" + "a" * 64 + "/source.m4a"
    artifacts.delete(key)
    bucket.delete_object.assert_called_once_with(key)
    bucket.get_bucket_versioning.return_value.status = "Enabled"
    with pytest.raises(ValueError):
        artifacts.ensure_unversioned()


def test_concurrent_job_reservation_prevents_deletion(system):
    service, catalog, _ = system
    audio, _ = completed(service)
    original = service.records.put

    def racing_put(kind, key, value, revision=None):
        if kind == "audio" and value.get("state") == "deleting":
            service.records.put = original
            service.reserve_media(audio["id"], "concurrent-job", 1)
        return original(kind, key, value, revision)

    service.records.put = racing_put
    with pytest.raises(Conflict):
        cleanup(catalog, request())
    service.artifacts.delete.assert_not_called()


def test_local_queue_blocks_cleanup_and_export_works_without_audio(system, tmp_path):
    from fastapi.testclient import TestClient

    from transcript_cloud.local import create_app
    from transcript_cloud.local_stage import execute
    from transcript_cloud.models import StudioSettings

    service, catalog, cloud = system
    _, prior = completed(service)
    app = create_app(tmp_path, cloud=cloud, start_worker=False)
    store = app.state.store
    job = store.create("export", [{"id": identity(URL)}], StudioSettings().model_dump())
    response = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/cleanup", json=request().model_dump()
    )
    assert response.status_code == 400
    service.artifacts.delete.assert_not_called()
    cleanup(catalog, request())
    result = execute(store, job, job["items"][0], cloud)
    assert result["remote_ids"] == [prior["id"]]
    service.step(prior["id"])
    assert service.require(prior["id"])["state"] == "completed"
    assert service.provider.submit.call_count == 1
