"""Offline scheduler checks: no media, network or model calls."""

import json

import pytest

from bilibili_transcript.web.store import Store
from bilibili_transcript.web.worker import Worker, run_item


class Child:
    def __init__(self, *args, **kwargs):
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.import_urls([{"url": f"https://www.bilibili.com/video/BV{i}"} for i in range(1, 9)])
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    store.execute("UPDATE resources SET audio=?", (json.dumps([{"path": str(audio)}]),))
    config = store.settings()
    config["parser"]["provider"] = "asr_flash"
    worker = Worker(store)
    monkeypatch.setattr("bilibili_transcript.web.worker.subprocess.Popen", Child)
    monkeypatch.setattr(worker, "terminate", lambda child: setattr(child, "returncode", -1))
    yield store, worker, config
    for entry in worker.active.values():
        entry["log"].close()


def finish(store, worker, item_id, result=None, pending=False):
    store.execute(
        "UPDATE items SET state=?,result=? WHERE id=?",
        ("pending" if pending else "completed", json.dumps(result or {}), item_id),
    )
    worker.active[item_id]["process"].returncode = 0


def test_download_is_serial_while_cloud_items_overlap(setup):
    store, worker, config = setup
    download = store.create_job("download", [1, 2], config)
    parse = store.create_job("parse", [3, 4, 5, 6, 7], config)
    worker.tick()
    assert sum(e["lane"] == "scraping" for e in worker.active.values()) == 1
    assert sum(e["lane"] == "processing" for e in worker.active.values()) == 4
    assert store.job(download)["items"][1]["state"] == "pending"
    assert store.job(parse)["items"][-1]["state"] == "pending"
    first = store.job(download)["items"][0]["id"]
    finish(store, worker, first)
    worker.tick()
    assert store.job(download)["items"][1]["state"] == "running"


def test_pipeline_releases_scraping_slot_and_preserves_resource_lock(setup):
    store, worker, config = setup
    job_id = store.create_job("pipeline", [1, 2], config)
    duplicate = store.create_job("parse", [1], config)
    worker.tick()
    first = store.job(job_id)["items"][0]["id"]
    finish(store, worker, first, {"download": {"audio_parts": 1}}, pending=True)
    worker.tick()
    assert worker.active[first]["lane"] == "processing"
    assert sum(e["lane"] == "scraping" for e in worker.active.values()) == 1
    assert store.job(duplicate)["items"][0]["state"] == "pending"


def test_missing_dependencies_wait_but_unrelated_work_starts(setup):
    store, worker, config = setup
    store.execute("UPDATE resources SET audio='[]' WHERE id=1")
    parse = store.create_job("parse", [1], config)
    download = store.create_job("download", [1], config)
    other = store.create_job("parse", [2], config)
    worker.tick()
    assert store.job(parse)["items"][0]["state"] == "pending"
    assert store.job(download)["items"][0]["state"] == "running"
    assert store.job(other)["items"][0]["state"] == "running"


def test_pause_drains_and_cancel_stops_all_children(setup):
    store, worker, config = setup
    job_id = store.create_job("parse", [1, 2, 3, 4, 5], config)
    worker.tick()
    store.execute("UPDATE jobs SET state='pausing' WHERE id=?", (job_id,))
    active = list(worker.active)
    for item_id in active:
        finish(store, worker, item_id)
    worker.tick()
    assert store.job(job_id)["state"] == "paused"
    assert store.job(job_id)["items"][-1]["state"] == "pending"
    other = store.create_job("parse", [6, 7], config)
    worker.tick()
    children = [e["process"] for e in worker.active.values()]
    store.execute("UPDATE jobs SET state='cancelling' WHERE id=?", (other,))
    worker.tick()
    assert all(c.poll() == -1 for c in children)
    assert store.job(other)["state"] == "cancelled"
    assert all(i["state"] == "cancelled" for i in store.job(other)["items"])


def test_local_whisper_is_serial_and_does_not_block_exports(setup):
    store, worker, config = setup
    config["parser"]["provider"] = "whisper"
    store.create_job("parse", [1, 2], config)
    store.create_job("export", [3], config, dry_run=True)
    worker.tick()
    assert [e["lane"] for e in worker.active.values()].count("local") == 1
    assert [e["lane"] for e in worker.active.values()].count("processing") == 1


def test_pipeline_child_checkpoints_each_stage(setup, monkeypatch):
    store, worker, config = setup
    job_id = store.create_job("pipeline", [1], config, force=True)
    item_id = store.job(job_id)["items"][0]["id"]
    calls = []

    def execute(store, job, item):
        calls.append((job["kind"], job["force"]))
        return {job["kind"]: {"ok": True}}

    monkeypatch.setattr("bilibili_transcript.web.stages.execute", execute)
    for stage in ("download", "parse", "export"):
        run_item(store.root, job_id, item_id, stage)
        item = store.job(job_id)["items"][0]
        assert item["state"] == ("completed" if stage == "export" else "pending")
    assert list(item["result"]) == ["download", "parse", "export"]
    assert calls == [(s, True) for s in ("download", "parse", "export")]


def test_spawn_failure_does_not_block_other_items(setup, monkeypatch):
    store, worker, config = setup
    calls = []

    def spawn(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("spawn unavailable")
        return Child()

    monkeypatch.setattr("bilibili_transcript.web.worker.subprocess.Popen", spawn)
    job_id = store.create_job("parse", [1, 2], config)
    worker.tick()
    assert [i["state"] for i in store.job(job_id)["items"]] == ["failed", "running"]


def test_retry_preserves_pipeline_stage_checkpoint(tmp_path):
    from fastapi.testclient import TestClient
    from bilibili_transcript.web.app import create_app

    app = create_app(tmp_path, start_worker=False)
    store = app.state.store
    store.import_urls([{"url": "https://www.bilibili.com/video/BV123"}])
    job_id = store.create_job("pipeline", [1], store.settings(), force=True)
    store.execute("UPDATE jobs SET state='failed' WHERE id=?", (job_id,))
    store.execute(
        "UPDATE items SET state='failed',result=? WHERE job_id=?",
        (json.dumps({"download": {"audio_parts": 1}}), job_id),
    )
    with TestClient(app) as client:
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
    item = store.one("SELECT * FROM items WHERE job_id=?", (job_id,))
    assert Worker.next_stage(store.job(job_id), item) == "parse"
