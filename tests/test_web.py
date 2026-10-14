"""Small, offline checks of persistence, controls, and stage boundaries."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bilibili_transcript.web.app import create_app
from bilibili_transcript.web.models import Settings
from bilibili_transcript.web.output import timestamp
from bilibili_transcript.web.stages import execute
from bilibili_transcript.web.store import Store
from bilibili_transcript.web.worker import Worker

URL = "https://www.bilibili.com/video/BV123"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, start_worker=False)) as client:
        yield client


def test_import_is_atomic_normalized_and_many_to_many(client):
    response = client.post("/api/import/links", json={"urls": [URL + "?share_source=x", URL]})
    assert response.json()["added"] == 1
    assert client.post("/api/import/links", json={"urls": [URL]}).json()["existing"] == 1
    assert (
        client.post(
            "/api/import/links", json={"urls": [URL, "http://localhost/private"]}
        ).status_code
        == 422
    )
    store = client.app.state.store
    store.import_urls(
        [{"url": URL, "title": "Lecture"}],
        collection={"name": "Lectures", "url": "https://space.bilibili.com/1/lists/2?type=series"},
    )
    library = client.get("/api/library").json()
    assert len(library["resources"]) == 1
    assert library["resources"][0]["collections"] == [1, 2]
    assert library["resources"][0]["title"] == "Lecture"


def test_job_snapshot_and_control_transitions(client):
    client.post("/api/import/links", json={"urls": [URL]})
    job_id = client.post("/api/jobs", json={"kind": "parse", "resource_ids": [1, 1]}).json()["id"]
    settings = Settings().model_dump()
    settings["parser"]["model"] = "tiny"
    assert client.put("/api/settings", json=settings).status_code == 200
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["config"]["parser"]["model"] == "base"
    assert len(job["items"]) == 1
    assert client.post(f"/api/jobs/{job_id}/pause").json()["state"] == "paused"
    assert client.post(f"/api/jobs/{job_id}/resume").json()["state"] == "queued"
    assert client.post(f"/api/jobs/{job_id}/cancel").json()["state"] == "cancelled"
    assert client.post(f"/api/jobs/{job_id}/resume").status_code == 409
    assert client.post(f"/api/jobs/{job_id}/retry").json()["state"] == "queued"
    assert client.get(f"/api/jobs/{job_id}").json()["items"][0]["state"] == "pending"


def test_dry_run_detects_missing_inputs_without_network_or_model(client):
    store = client.app.state.store
    store.import_urls([{"url": URL}])
    job_id = store.create_job("parse", [1], store.settings(), dry_run=True)
    job = store.job(job_id)
    with (
        patch("bilibili_audio_scraper.AudioScraper") as scraper,
        patch("bilibili_transcript.parser.WhisperParser") as parser,
    ):
        result = execute(store, job, job["items"][0])
        assert "blocked" in result["steps"][0]["action"]
        scraper.assert_not_called()
        parser.assert_not_called()
    assert store.one("SELECT audio,parsed,outputs FROM resources WHERE id=1") == {
        "audio": "[]",
        "parsed": "[]",
        "outputs": "[]",
    }


def test_export_consumes_saved_json_only_and_serves_files(client):
    store = client.app.state.store
    store.import_urls([{"url": URL}])
    path = store.root / "parsed" / "fixture.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {"title": "Sample", "segments": [{"start": 59.9996, "end": 61.0, "text": "Hello 世界"}]}
        ),
        encoding="utf-8",
    )
    store.execute("UPDATE resources SET parsed=? WHERE id=1", (json.dumps([str(path)]),))
    settings = store.settings()
    settings["output"]["formats"] = ["txt", "vtt", "srt", "json"]
    job = store.job(store.create_job("export", [1], settings))
    with patch("bilibili_transcript.parser.WhisperParser") as parser:
        result = execute(store, job, job["items"][0])
        assert result["export"]["files"] == 4
        parser.assert_not_called()
    response = client.get("/api/resources/1/files/outputs/2")
    assert response.status_code == 200
    assert "00:01:00,000 --> 00:01:01,000" in response.text
    assert "Hello 世界" in response.text
    assert timestamp(3599.9996) == "01:00:00.000"
    assert client.get("/api/resources/1/files/outputs/-1").status_code == 404


def test_parse_missing_audio_fails_before_model_load(client):
    store = client.app.state.store
    store.import_urls([{"url": URL}])
    job = store.job(store.create_job("parse", [1], store.settings()))
    with patch("bilibili_transcript.parser.WhisperParser") as parser:
        with pytest.raises(ValueError, match="Download"):
            execute(store, job, job["items"][0])
        parser.assert_not_called()


def test_restart_preserves_completed_items(tmp_path):
    store = Store(tmp_path)
    store.import_urls([{"url": URL}, {"url": URL + "4"}])
    job_id = store.create_job("download", [1, 2], store.settings())
    store.execute("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
    store.execute("UPDATE items SET state='completed',progress=100 WHERE resource_id=1")
    store.execute("UPDATE items SET state='running',progress=42 WHERE resource_id=2")
    worker = Worker(store)
    worker.start()
    worker.close()
    job = store.job(job_id)
    assert job["state"] == "paused"
    assert [i["state"] for i in job["items"]] == ["completed", "pending"]


def test_real_worker_runs_dry_run_subprocess(tmp_path):
    store = Store(tmp_path)
    store.import_urls([{"url": URL}])
    job_id = store.create_job("pipeline", [1], store.settings(), dry_run=True)
    worker = Worker(store)
    worker.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = store.job(job_id)
            if job["state"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert job["state"] == "completed", job
        assert len(job["items"][0]["result"]["steps"]) == 3
        assert not (store.root / "audio").exists()
    finally:
        worker.close()


def test_api_validation_and_cross_origin_protection(client):
    assert (
        client.post("/api/jobs", json={"kind": "download", "resource_ids": [999]}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/import/links", json={"urls": [URL]}, headers={"Origin": "https://example.com"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/import/collection", json={"name": "Bad", "url": "https://example.com/1/lists/2"}
        ).status_code
        == 422
    )
    settings = Settings().model_dump()
    settings["output"]["formats"] = []
    assert client.put("/api/settings", json=settings).status_code == 422


def test_cancel_terminates_active_child_and_keeps_completed_items(tmp_path):
    import subprocess
    import sys

    app = create_app(tmp_path, start_worker=False)
    store = app.state.store
    store.import_urls([{"url": URL}, {"url": URL + "4"}])
    job_id = store.create_job("download", [1, 2], store.settings())
    store.execute("UPDATE items SET state='completed',progress=100 WHERE resource_id=1")
    real_popen = subprocess.Popen
    children = []

    def slow_child(command, **kwargs):
        child = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        return child

    worker = Worker(store)
    with patch("bilibili_transcript.web.worker.subprocess.Popen", side_effect=slow_child):
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not children and time.monotonic() < deadline:
                time.sleep(0.05)
            assert children
            # Restore Popen so Windows taskkill can launch normally.
            with patch("bilibili_transcript.web.worker.subprocess.Popen", real_popen):
                with TestClient(app) as client:
                    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
                while time.monotonic() < deadline:
                    job = store.job(job_id)
                    if job["state"] == "cancelled":
                        break
                    time.sleep(0.05)
                assert job["state"] == "cancelled"
                assert [item["state"] for item in job["items"]] == ["completed", "cancelled"]
                assert children[0].poll() is not None
        finally:
            worker.close()
