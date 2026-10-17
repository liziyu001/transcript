import json
import zipfile
from unittest.mock import patch

import httpx
import pytest
import test_studio
from fastapi.testclient import TestClient
from test_studio import COLLECTION, URL

from transcript_cloud.bundles import BundleRequest, build_archive, bundle_plan, component
from transcript_cloud.catalog import ImportBatch, ResourceEdit, identity
from transcript_cloud.cleanup import CleanupRequest, cleanup
from transcript_cloud.local import create_app
from transcript_cloud.models import AudioRegistration, NewJob


@pytest.fixture
def system():
    return test_studio.system.__wrapped__()


def prepare(service, catalog):
    catalog.import_batch(
        ImportBatch(
            entries=[
                {
                    "url": URL,
                    "title": "课程 / 视频一",
                    "metadata": {"pages": [{"page": 3, "part": "第三讲：实践"}]},
                }
            ],
            collection_url=COLLECTION,
            name="课程合集",
        )
    )
    aids = []
    for digit, number in [("a", 3), ("b", 7)]:
        audio = service.register(
            AudioRegistration(
                sha256=digit * 64,
                key=f"audio/{digit * 64}/source.m4a",
                size=10,
                title=f"分段标题 {number}",
                source_url=URL + f"?p={number}",
            )
        )
        aids.append(audio["id"])
        job = service.create(NewJob(audio_id=audio["id"], request_id="bundle-test"))
        service.step(job["id"])
        service.step(job["id"])
    catalog.edit(identity(URL), ResourceEdit(audio_ids=aids))
    return aids


def test_bundle_has_collection_video_and_actual_part_even_after_audio_deleted(system):
    service, catalog, cloud = system
    prepare(service, catalog)
    cleanup(catalog, CleanupRequest(resource_ids=[identity(URL)], category="audio", confirmed=True))
    plan = cloud.request(
        "POST", "/studio/download-plan", {"resource_ids": [identity(URL)], "formats": ["vtt"]}
    )
    paths = [f["path"] for f in plan["files"]]
    assert len(paths) == 2 and not plan["missing"]
    assert all(
        "课程合集" in path and "BV1test123" in path and "课程 _ 视频一" in path for path in paths
    )
    assert "P003_第三讲：实践" in paths[0]
    assert "P007_分段标题 7" in paths[1]
    assert all(len(path.split("/")) == 3 for path in paths)
    assert all(f["source_url"].startswith(URL + "?p=") for f in plan["files"])


def test_multiple_collections_and_missing_formats(system):
    service, catalog, _ = system
    aids = prepare(service, catalog)
    other = "https://space.bilibili.com/123/lists/789?type=series"
    catalog.import_batch(ImportBatch(entries=[{"url": URL}], collection_url=other, name="课程合集"))
    latest = service.create(NewJob(audio_id=aids[0], request_id="new-export"))
    service.step(latest["id"])
    service.step(latest["id"])
    latest = service.require(latest["id"])
    latest["outputs"].pop("vtt")
    service.save(latest)
    plan = bundle_plan(catalog, BundleRequest(resource_ids=[identity(URL)], formats=["txt", "vtt"]))
    assert len(plan["files"]) == 6 and len(plan["missing"]) == 1
    assert len({f["path"].casefold() for f in plan["files"]}) == 6
    chosen = bundle_plan(
        catalog,
        BundleRequest(resource_ids=[identity(URL)], formats=["txt"], collection_id=identity(other)),
    )
    assert len(chosen["files"]) == 2
    assert all(f["collection_id"] == identity(other) for f in chosen["files"])
    with pytest.raises(ValueError):
        bundle_plan(catalog, BundleRequest(resource_ids=[identity(URL)], collection_id="missing"))


def test_no_outputs_reports_error(system):
    _, catalog, _ = system
    catalog.import_batch(ImportBatch(entries=[{"url": URL}]))
    with pytest.raises(ValueError, match="没有可下载"):
        bundle_plan(catalog, BundleRequest(resource_ids=[identity(URL)]))


@pytest.mark.parametrize("name", ["../bad\\name", "CON", "nul.txt", "a:*?<>|", " . ", "\x00evil"])
def test_portable_components(name):
    result = component(name)
    assert "/" not in result and "\\" not in result and ".." not in result
    assert result and not result.endswith((".", " "))


def test_zip_manifest_and_local_response_cleanup(system, tmp_path):
    service, catalog, cloud = system
    prepare(service, catalog)
    app = create_app(tmp_path, cloud=cloud, start_worker=False)
    real_client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content="WEBVTT\n中文字幕".encode())
    )
    with patch(
        "transcript_cloud.bundles.httpx.Client",
        side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
    ):
        response = TestClient(app, base_url="http://127.0.0.1").post(
            "/api/download-results", json={"resource_ids": [identity(URL)], "formats": ["vtt"]}
        )
    assert response.status_code == 200
    import io

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len([n for n in archive.namelist() if n.endswith(".vtt")]) == 2
        manifest = json.loads(archive.read("下载清单.json"))
        assert "url" not in manifest["files"][0] and manifest["files"][0]["part"] == 3
        assert archive.read(manifest["files"][0]["path"]).startswith(b"WEBVTT")
    assert not list((tmp_path / "bundles").glob("*.zip"))


@pytest.mark.parametrize("status,max_bytes", [(404, 1000), (200, 2)])
def test_failed_download_removes_temp_archive(tmp_path, status, max_bytes):
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=b"transcript"))
    plan = {
        "files": [{"path": "collection/video/part.vtt", "url": "https://example.test/signed"}],
        "missing": [],
    }
    with (
        patch(
            "transcript_cloud.bundles.httpx.Client",
            side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
        ),
        pytest.raises(ValueError),
    ):
        build_archive(plan, tmp_path, max_bytes=max_bytes)
    assert not list(tmp_path.glob("*.zip"))


def test_long_unicode_names_fit_filesystem_limits(system):
    service, catalog, _ = system
    prepare(service, catalog)
    catalog.edit(
        identity(URL),
        ResourceEdit(
            title="中文标题" * 100, metadata={"pages": [{"page": 3, "part": "中文分段" * 100}]}
        ),
    )
    plan = bundle_plan(catalog, BundleRequest(resource_ids=[identity(URL)]))
    for entry in plan["files"]:
        assert all(len(part.encode("utf-8")) <= 255 for part in entry["path"].split("/"))
