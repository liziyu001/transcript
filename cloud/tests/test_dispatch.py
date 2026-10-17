from unittest.mock import Mock, patch

from transcript_cloud.ingest import atomic_json
from transcript_cloud.local_store import LocalStore
from transcript_cloud.local_worker import Worker
from transcript_cloud.models import StudioSettings


def setup(tmp_path):
    store = LocalStore(tmp_path)
    cloud = Mock()
    cloud.all.return_value = []
    worker = Worker(store, cloud)

    def create(kind, ids):
        return store.create(kind, [{"id": rid} for rid in ids], StudioSettings().model_dump())

    return store, worker, create


def process():
    child = Mock()
    child.poll.return_value = None
    return child


def close_logs(worker):
    for active in worker.processes.values():
        active["log"].close()


def test_cloud_dispatch_does_not_wait_for_download_and_has_no_job_cap(tmp_path):
    store, worker, create = setup(tmp_path)
    download = create("download", ["downloading", "next-download"])
    with patch(
        "transcript_cloud.local_worker.subprocess.Popen", side_effect=lambda *a, **kw: process()
    ):
        worker.tick()
        cloud = create("parse", [f"ready-{n}" for n in range(20)])
        export = create("export", ["finished"])
        worker.tick()
    try:
        assert len(worker.processes) == 22
        assert sum(a["transfer"] for a in worker.processes.values()) == 1
        assert store.job(download["id"])["items"][1]["state"] == "pending"
        assert all(i["state"] == "running" for i in store.job(cloud["id"])["items"])
        assert store.job(export["id"])["items"][0]["state"] == "running"
    finally:
        close_logs(worker)


def test_pipeline_upload_hands_off_and_next_upload_starts(tmp_path):
    store, worker, create = setup(tmp_path)
    job = create("pipeline", ["first", "second"])
    first = job["items"][0]["id"]
    with patch(
        "transcript_cloud.local_worker.subprocess.Popen", side_effect=lambda *a, **kw: process()
    ) as spawn:
        worker.tick()
        assert spawn.call_args.args[0][-1] == "--upload-only"
        atomic_json(store.root / "items" / first / "result.json", {"audio_ids": ["a"]})
        worker.processes[first]["process"].poll.return_value = 0
        worker.tick()
    try:
        assert len(worker.processes) == 2
        assert not worker.processes[first]["transfer"]
        assert store.job(job["id"])["items"][0]["stage"] == "parse"
        assert sum(a["transfer"] for a in worker.processes.values()) == 1
    finally:
        close_logs(worker)


def test_cancel_is_scoped_and_waiting_cloud_does_not_block_other_dispatch(tmp_path):
    store, worker, create = setup(tmp_path)
    download = create("download", ["first"])
    parsing = create("parse", ["second"])
    with patch(
        "transcript_cloud.local_worker.subprocess.Popen", side_effect=lambda *a, **kw: process()
    ):
        worker.tick()
    try:
        with patch.object(worker, "stop_process") as stop:
            worker.terminate(parsing["id"])
            stop.assert_called_once_with(worker.processes[parsing["items"][0]["id"]]["process"])
        assert download["items"][0]["id"] in worker.processes
    finally:
        close_logs(worker)


def test_same_resource_submission_waits_only_until_dispatch_finishes(tmp_path):
    store, worker, create = setup(tmp_path)
    first = create("parse", ["same"])
    second = create("parse", ["same"])
    iid = first["items"][0]["id"]
    with patch(
        "transcript_cloud.local_worker.subprocess.Popen", side_effect=lambda *a, **kw: process()
    ):
        worker.tick()
        assert len(worker.processes) == 1
        atomic_json(store.root / "items" / iid / "result.json", {"remote_ids": ["remote-job"]})
        worker.processes[iid]["process"].poll.return_value = 0
        worker.tick()
    try:
        assert store.job(first["id"])["items"][0]["state"] == "waiting"
        assert second["items"][0]["id"] in worker.processes
    finally:
        close_logs(worker)
