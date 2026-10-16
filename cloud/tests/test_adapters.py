"""Check real SDK call shapes with transports mocked at the boundary."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import tablestore as ots
from transcript_cloud.ingest import upload
from transcript_cloud.models import AudioRegistration, Settings
from transcript_cloud.storage import Artifacts, Conflict, Records


def settings():
    return Settings(bucket="test", ots_instance="test", flow="test", panel_token="x" * 32)


def test_tablestore_revision_and_pagination():
    records = Records(settings())
    client = Mock()
    records.client = lambda: client
    row = ots.Row(
        [("kind", "job"), ("id", "a")],
        [("document", json.dumps({"id": "a", "state": "queued"}), 1), ("revision", 2, 1)],
    )
    client.get_row.return_value = (None, row, None)
    assert records.get("job", "a")["revision"] == 2
    records.put("job", "a", {"state": "running"}, revision=2)
    args = client.put_row.call_args.args
    assert args[0] == "transcript_records"
    assert args[1].attribute_columns[1] == ("revision", 3)
    condition = args[2]
    assert condition.row_existence_expectation == ots.RowExistenceExpectation.EXPECT_EXIST
    client.get_range.return_value = (None, [("kind", "job"), ("id", "b")], [row], None)
    page = records.list("job")
    assert page["cursor"] == "b"
    assert page["items"][0]["state"] == "queued"
    client.put_row.side_effect = ots.OTSServiceError(409, "OTSConditionCheckFail", "changed")
    with pytest.raises(Conflict):
        records.put("job", "a", {}, revision=1)


def test_registration_checks_size_and_hash():
    artifacts = Artifacts(settings())
    bucket = Mock()
    artifacts.bucket = lambda: bucket
    sha = "a" * 64
    audio = AudioRegistration(sha256=sha, key=f"audio/{sha}/source.mp3", title="test", size=10)
    bucket.head_object.return_value = SimpleNamespace(
        content_length=10, headers={"x-oss-meta-sha256": sha}
    )
    artifacts.verify(audio)
    bucket.head_object.return_value.content_length = 9
    with pytest.raises(ValueError, match="size"):
        artifacts.verify(audio)


def test_completed_upload_is_reused_after_local_manifest_loss(tmp_path, monkeypatch):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"test audio")
    monkeypatch.setattr("transcript_cloud.ingest.duration", lambda _: 10)
    from transcript_cloud.ingest import digest

    sha = digest(path)
    bucket = Mock()
    bucket.head_object.return_value = SimpleNamespace(
        content_length=path.stat().st_size, headers={"x-oss-meta-sha256": sha}
    )
    uploader = Mock()
    monkeypatch.setattr("oss2.resumable_upload", uploader)
    _, manifest = upload(path, bucket, tmp_path / "state")
    uploader.assert_not_called()
    assert manifest["uploaded"] is True
    assert manifest["audio"]["sha256"] == sha


def test_create_and_update_ros_requests_preserve_parameters():
    from alibabacloud_ros20190910.models import CreateStackRequest, UpdateStackRequest

    data = {
        "RegionId": "cn-beijing",
        "TemplateBody": "{}",
        "TimeoutInMinutes": 30,
        "Parameters": [{"ParameterKey": "PanelToken", "ParameterValue": "dummy"}],
    }
    for model in (CreateStackRequest, UpdateStackRequest):
        request = model().from_map(copy.deepcopy(data))
        assert request.parameters[0].parameter_value == "dummy"
        assert request.template_body == "{}"
