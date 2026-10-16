"""Offline metadata normalization, migration and ingestion checks; no media downloads."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from bilibili_audio_scraper.metadata import parse_video_metadata
from bilibili_transcript.web.app import create_app
from bilibili_transcript.web.stages import execute
from bilibili_transcript.web.store import Store

URL = "https://www.bilibili.com/video/BV1QcLTzfEj8"


def example():
    # Reduced fixture using the example video's verified part IDs and durations.
    return {
        "bvid": "BV1QcLTzfEj8",
        "title": "Example livestream",
        "videos": 2,
        "duration": 15243,
        "pages": [
            {"page": 1, "cid": 29525806161, "part": "Part one", "duration": 7230},
            {"page": 2, "cid": 29525806257, "part": "Part two", "duration": 8013},
        ],
    }


def test_multipart_duration_is_sum_not_first_part():
    data = example()
    data["duration"] = 7230  # The parts are authoritative even if the summary is wrong.
    metadata = parse_video_metadata(data)
    assert metadata["duration"] == 15243
    assert metadata["part_count"] == 2
    assert metadata["pages"][1] == {
        "page": 2,
        "cid": 29525806257,
        "title": "Part two",
        "duration": 8013,
    }


def test_single_part_and_unknown_duration():
    data = {"videos": 1, "pages": [{"page": 1, "part": "Only part", "duration": 90}]}
    assert parse_video_metadata(data)["duration"] == 90
    del data["pages"][0]["duration"]
    metadata = parse_video_metadata(data)
    assert metadata["duration"] is None
    assert metadata["pages"][0]["duration"] is None
    data["duration"] = 90
    assert parse_video_metadata(data)["duration"] == 90


def test_incomplete_parts_do_not_produce_misleading_total():
    data = example()
    data["pages"].pop()
    with pytest.raises(RuntimeError, match="incomplete"):
        parse_video_metadata(data)


def test_existing_library_migration_preserves_artifacts(tmp_path):
    with sqlite3.connect(tmp_path / "library.sqlite3") as db:
        db.execute(
            "CREATE TABLE resources (id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, "
            "title TEXT NOT NULL, audio TEXT NOT NULL DEFAULT '[]', "
            "parsed TEXT NOT NULL DEFAULT '[]', outputs TEXT NOT NULL DEFAULT '[]', "
            "created TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO resources VALUES (1, ?, 'Original', '[\"audio.m4a\"]', "
            "'[\"transcript.json\"]', '[\"export.srt\"]', '2026-01-01')",
            (URL,),
        )
    store = Store(tmp_path)
    Store(tmp_path)  # Idempotent on every worker startup.
    before = store.one("SELECT * FROM resources WHERE id=1")
    assert before["metadata"] == "{}"
    store.save_metadata(1, parse_video_metadata(example()))
    after = store.one("SELECT * FROM resources WHERE id=1")
    assert after["title"] == "Example livestream"
    for field in ("audio", "parsed", "outputs", "created"):
        assert before[field] == after[field]
    assert json.loads(after["metadata"])["duration"] == 15243


def test_link_import_queues_metadata_and_library_exposes_it(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    store = app.state.store
    with TestClient(app) as client:
        result = client.post("/api/import/links", json={"urls": [URL]}).json()
        job = store.job(result["metadata_job_id"])
        assert job["kind"] == "metadata"
        assert len(job["items"]) == 1
        assert client.get("/api/library").json()["resources"][0]["metadata"] == {}
        again = client.post("/api/import/links", json={"urls": [URL]}).json()
        assert again["metadata_job_id"] is None
        store.save_metadata(1, parse_video_metadata(example()))
        resource = client.get("/api/library").json()["resources"][0]
        assert resource["metadata"]["duration"] == 15243
        assert len(resource["metadata"]["pages"]) == 2
        assert resource["audio"] == resource["parsed"] == resource["outputs"] == []
        refresh = client.post(
            "/api/jobs", json={"kind": "metadata", "resource_ids": [1], "dry_run": True}
        )
        job = store.job(refresh.json()["id"])
        result = execute(store, job, job["items"][0])
        assert result["steps"][0]["stage"] == "metadata"
        assert not (tmp_path / "audio").exists()


def test_link_import_failed_lookup_can_be_requeued(tmp_path):
    store = Store(tmp_path)
    result = store.import_urls([{"url": URL}], resolve_metadata=True)
    job_id = result["metadata_job_id"]
    assert store.job(job_id)["items"][0]["resource_id"] == 1
    store.execute("UPDATE jobs SET state='failed' WHERE id=?", (job_id,))
    store.execute("UPDATE items SET state='failed' WHERE job_id=?", (job_id,))
    store.execute("UPDATE resources SET metadata_error='Unavailable' WHERE id=1")
    result = store.import_urls([{"url": URL}], resolve_metadata=True)
    assert result["metadata_job_id"] != job_id
    store.save_metadata(1, parse_video_metadata(example()))
    assert store.one("SELECT metadata_error FROM resources WHERE id=1")["metadata_error"] == ""
    result = store.import_urls([{"url": URL}], resolve_metadata=True)
    assert result["metadata_job_id"] is None


def test_collection_resolve_saves_durations_without_detail_jobs(tmp_path, monkeypatch):
    from bilibili_audio_scraper import collection

    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            page = calls[-1][1]["pn"]
            archives = [
                {"bvid": f"BV{i}", "title": f"Replay {i}", "duration": 12428}
                for i in (range(30) if page == 1 else range(30, 31))
            ]
            if page == 2:
                archives[0].pop("duration")
            return {"code": 0, "data": {"archives": archives, "page": {"total": 31}}}

    class Session:
        def mount(self, *args):
            pass

        def get(self, url, params, **kwargs):
            calls.append((url, params))
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(collection.requests, "Session", Session)
    app = create_app(tmp_path, start_worker=False)
    store = app.state.store
    config = store.settings()
    config["collection"] = {
        "name": "Replay",
        "url": "https://space.bilibili.com/45502/lists/31284?type=series",
    }
    job = store.job(store.create_job("discover", [None], config))
    execute(store, job, job["items"][0])
    assert [params["pn"] for _, params in calls] == [1, 2]
    assert all(url.endswith("/x/series/archives") for url, _ in calls)
    assert store.rows("SELECT id FROM jobs WHERE kind='metadata'") == []
    with TestClient(app) as client:
        resources = client.get("/api/library").json()["resources"]
    assert len(resources) == 31
    known = next(r for r in resources if r["title"] == "Replay 0")
    assert known["metadata"]["duration"] == 12428
    assert "part_count" not in known["metadata"]
    unknown = next(r for r in resources if r["title"] == "Replay 30")
    assert unknown["metadata"].get("duration") is None
    assert not (tmp_path / "audio").exists()


def test_collection_summary_preserves_details_and_artifacts(tmp_path):
    store = Store(tmp_path)
    store.import_urls([{"url": URL}])
    store.save_metadata(1, parse_video_metadata(example()))
    store.execute(
        "UPDATE resources SET audio='[\"audio.m4a\"]', parsed='[\"parsed.json\"]', outputs='[\"out.srt\"]' WHERE id=1"
    )
    before = store.one("SELECT * FROM resources WHERE id=1")
    for duration in (15243, None):
        store.import_urls(
            [
                {
                    "url": URL,
                    "title": "Updated title",
                    "metadata": {
                        "title": "Updated title",
                        "duration": duration,
                    },
                }
            ]
        )
    after = store.one("SELECT * FROM resources WHERE id=1")
    metadata = json.loads(after["metadata"])
    assert metadata["duration"] == 15243
    assert metadata["part_count"] == 2
    assert metadata["pages"] == json.loads(before["metadata"])["pages"]
    assert after["title"] == metadata["title"] == "Updated title"
    for field in ("audio", "parsed", "outputs"):
        assert after[field] == before[field]
    assert store.rows("SELECT id FROM jobs") == []
