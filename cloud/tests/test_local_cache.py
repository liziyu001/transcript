import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_studio
from test_studio import URL

from transcript_cloud.catalog import ImportBatch, identity
from transcript_cloud.ingest import atomic_json
from transcript_cloud.local_cache import remove_uploaded_audio, sweep_uploaded_audio
from transcript_cloud.local_client import CloudError
from transcript_cloud.local_stage import execute
from transcript_cloud.local_store import LocalStore
from transcript_cloud.models import StudioSettings


@pytest.mark.parametrize("failure", ["upload", "link", None])
def test_media_deleted_only_after_complete_upload_and_replay(tmp_path, failure):
    _, catalog, cloud = test_studio.system.__wrapped__()
    catalog.import_batch(ImportBatch(entries=[{"url": URL}]))
    store = LocalStore(tmp_path)
    job = store.create("download", [{"id": identity(URL)}], StudioSettings().model_dump())
    item = job["items"][0]
    root = store.root / "items" / item["id"]
    folder = root / "audio"
    folder.mkdir(parents=True)
    sources = [
        SimpleNamespace(path=folder / f"p{i}.m4a", title=f"P{i}", url=URL + f"?p={i}")
        for i in (1, 2)
    ]
    for source in sources:
        source.path.write_bytes(b"audio")
    original = cloud.request

    def request(method, path, body=None):
        if failure == "link" and method == "PATCH":
            raise CloudError("link failed")
        return original(method, path, body)

    cloud.request = request

    def upload(path, bucket, state_dir, title, url):
        if failure == "upload" and title == "P2":
            raise RuntimeError("upload failed")
        digit = "a" if title == "P1" else "b"
        return path, {
            "audio": {
                "sha256": digit * 64,
                "key": f"audio/{digit * 64}/source.m4a",
                "size": 5,
                "title": title,
                "source_url": url,
            }
        }

    if failure:
        with pytest.raises((CloudError, RuntimeError)):
            execute(store, job, item, cloud, download=lambda _: sources, uploader=upload)
        assert all(s.path.exists() for s in sources)
        assert not json.loads((root / "checkpoint.json").read_text()).get("uploaded")
    failure = None
    result = execute(store, job, item, cloud, download=lambda _: sources, uploader=upload)
    assert len(result["audio_ids"]) == 2
    assert not folder.exists()
    assert (root / "checkpoint.json").exists()
    again = execute(
        store,
        job,
        item,
        cloud,
        download=Mock(side_effect=AssertionError),
        uploader=Mock(side_effect=AssertionError),
    )
    assert again == result


def test_sweep_only_uploaded_owned_media(tmp_path):
    store = LocalStore(tmp_path / "store")
    original = tmp_path / "original.m4a"
    original.write_bytes(b"keep")
    for uploaded in (True, False):
        job = store.create("download", [{"id": str(uploaded)}], StudioSettings().model_dump())
        root = store.root / "items" / job["items"][0]["id"]
        (root / "audio").mkdir(parents=True)
        (root / "audio" / "media.m4a").write_bytes(b"audio")
        atomic_json(
            root / "checkpoint.json", {"uploaded": uploaded, "sources": [{"path": str(original)}]}
        )
    assert sweep_uploaded_audio(store) == 1
    assert original.read_bytes() == b"keep"
    assert sweep_uploaded_audio(store) == 0
    assert len(list((store.root / "items").glob("*/audio/media.m4a"))) == 1


def test_reject_symlink_audio(tmp_path):
    root = tmp_path / "item"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.m4a").write_bytes(b"keep")
    atomic_json(root / "checkpoint.json", {"uploaded": True})
    try:
        (root / "audio").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks require privileges on this Windows host")
    with pytest.raises(ValueError):
        remove_uploaded_audio(root)
    assert (outside / "keep.m4a").exists()
