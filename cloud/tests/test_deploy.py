"""Deployment lifecycle contracts without cloud side effects."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from infra.app import synth
from ops.config import Config, initialize, read_env, write_values
from ops.deployment import Deployment, failure_message


@pytest.fixture
def config(tmp_path):
    path = tmp_path / ".env.local"
    write_values(
        path,
        {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "access-id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "access-secret",
            "TC_PANEL_TOKEN": "x" * 32,
            "TC_DASHSCOPE_KEY": "model-secret",
            "TC_BUCKET": "test-bucket",
            "TC_OTS_INSTANCE": "test-instance",
        },
    )
    return Config.load(path)


def test_env_file_wins_over_shell_and_quotes_remain_literal(config, monkeypatch):
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "wrong-shell-secret")
    monkeypatch.setenv("ALIBABA_CLOUD_SECURITY_TOKEN", "expired-shell-token")
    write_values(config.path, {"TC_DASHSCOPE_KEY": "literal $token # value"})
    loaded = Config.load(config.path)
    assert loaded.values["TC_DASHSCOPE_KEY"] == "literal $token # value"
    import os

    assert os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] == "access-secret"
    assert os.environ["ALIBABA_CLOUD_SECURITY_TOKEN"] == ""


def test_init_and_generated_panel_env_do_not_expose_model_key(config, tmp_path, monkeypatch):
    import ops.config as module

    path = tmp_path / "new.env"
    initialize(path)
    first = read_env(path)["TC_PANEL_TOKEN"]
    assert len(first) >= 24
    initialize(path)
    assert read_env(path)["TC_PANEL_TOKEN"] == first
    monkeypatch.setattr(module, "ROOT", tmp_path)
    config.panel_env("https://api.example.test")
    panel = read_env(tmp_path / ".panel.env")
    assert "TC_DASHSCOPE_KEY" not in panel
    assert panel["TC_API_URL"] == "https://api.example.test"
    assert read_env(config.path)["TC_DASHSCOPE_KEY"] == "model-secret"


def test_invalid_config_and_duplicate_keys_do_not_echo_secrets(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TC_PANEL_TOKEN=very-secret\nTC_PANEL_TOKEN=another-secret\n")
    with pytest.raises(ValueError) as error:
        Config.load(path)
    assert "secret" not in str(error.value)


def test_templates_have_no_hosting_or_obsolete_limits(tmp_path):
    synth(tmp_path)
    app = json.loads((tmp_path / "TranscriptApp.json").read_text())
    assert set(app["Parameters"]) == {
        "Prefix",
        "BucketName",
        "InstanceName",
        "CodeKey",
        "PanelToken",
        "DashScopeKey",
    }
    assert app["Parameters"]["PanelToken"]["NoEcho"]
    assert {"PATCH", "DELETE"} <= set(
        app["Resources"]["Http"]["Properties"]["TriggerConfig"]["methods"]
    )
    assert not (tmp_path / "TranscriptDomain.json").exists()
    storage = json.loads((tmp_path / "TranscriptStorage.json").read_text())
    assert all(r["DeletionPolicy"] == "Retain" for r in storage["Resources"].values())


class FakeROS:
    def __init__(self):
        self.stacks = {}
        self.created = []
        self.updated = []
        self.deleted = []

    def response(self, value):
        return SimpleNamespace(body=SimpleNamespace(to_map=lambda: copy.deepcopy(value)))

    def list_stacks(self, request):
        assert 1 <= request.page_size <= 50
        rows = [s for s in self.stacks.values() if s["StackName"] in request.stack_name]
        return self.response({"Stacks": rows, "TotalCount": len(rows)})

    def get_stack(self, request):
        return self.response(self.stacks[request.stack_id])

    def create_stack(self, request):
        stack_id = str(len(self.stacks) + 1)
        self.created.append(request)
        self.stacks[stack_id] = {
            "StackId": stack_id,
            "StackName": request.stack_name,
            "Status": "CREATE_COMPLETE",
            "Parameters": [p.to_map() for p in request.parameters],
            "Outputs": [{"OutputKey": "ApiUrl", "OutputValue": "https://api.example.test"}],
        }
        return SimpleNamespace(body=SimpleNamespace(stack_id=stack_id))

    def update_stack(self, request):
        self.updated.append(request)
        self.stacks[request.stack_id]["Status"] = "UPDATE_COMPLETE"
        return self.response({})

    def delete_stack(self, request):
        self.deleted.append(request.stack_id)
        self.stacks[request.stack_id]["Status"] = "DELETE_COMPLETE"
        return self.response({})


def deployment(config, monkeypatch):
    client = FakeROS()
    manager = Deployment(config, client=client, sleep=lambda _: None)
    manager.template = Mock(return_value={"Resources": {}})
    manager.manifest = Mock(return_value={"object_key": "code/test.zip"})
    manager.upload = Mock()
    monkeypatch.setattr(Config, "panel_env", Mock())
    return manager, client


def test_up_redeploy_down_up_preserves_storage_and_finds_stacks(config, monkeypatch):
    manager, client = deployment(config, monkeypatch)
    manager.up()
    assert len(client.created) == 2
    manager.up()
    assert len(client.created) == 2 and len(client.updated) == 1
    assert client.updated[0].stack_id == "2"
    manager.down()
    assert client.deleted == ["2"]
    assert client.stacks["1"]["Status"] == "CREATE_COMPLETE"
    manager.down()
    assert client.deleted == ["2"]
    manager.up()
    assert len(client.created) == 3 and client.created[-1].stack_name == "transcript-app"


def test_mismatched_storage_config_cannot_redeploy_or_destroy(config, monkeypatch):
    manager, client = deployment(config, monkeypatch)
    manager.up()
    client.stacks["1"]["Parameters"][0]["ParameterValue"] = "unrelated-bucket"
    with pytest.raises(ValueError, match="differs"):
        manager.up()
    client.stacks["2"]["Parameters"][0]["ParameterValue"] = "unrelated-bucket"
    with pytest.raises(ValueError, match="differs"):
        manager.down()
    assert not client.deleted and not client.updated


def test_wait_does_not_treat_rollback_as_success(config):
    manager = Deployment(config, client=Mock(), sleep=lambda _: None)
    manager.get = Mock(return_value={"Status": "ROLLBACK_COMPLETE"})
    with pytest.raises(ValueError, match="ROLLBACK_COMPLETE"):
        manager.wait("stack")


def test_sdk_diagnostic_redacts_env_and_templates(config):
    error = RuntimeError("access-id access-secret model-secret")
    error.code = "InvalidParameter"
    error.data = {"RequestId": "request-123"}
    message = failure_message(error)
    assert "request-123" in message
    assert not any(secret in message for secret in ("access-id", "access-secret", "model-secret"))
    assert "unknown-value" not in failure_message(RuntimeError("TemplateBody: unknown-value"))


def test_runtime_credentials_never_fall_back_to_cli_profile(monkeypatch):
    from transcript_cloud.storage import credentials

    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", raising=False)
    with pytest.raises(ValueError, match="Missing explicit"):
        credentials()


def test_existing_bucket_is_not_managed_by_storage_template(config, tmp_path, monkeypatch):
    from dataclasses import replace

    import ops.deployment as module

    synth(tmp_path / "ros.out")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    manager = Deployment(replace(config, existing_bucket=True), client=Mock())
    assert set(manager.template("storage")["Resources"]) == {"Instance", "Records"}


def test_manifest_rejects_changed_source_and_tampered_zip(config, tmp_path, monkeypatch):
    import hashlib

    import ops.deployment as module
    import scripts.build as builder

    artifacts = tmp_path / ".artifacts"
    artifacts.mkdir()
    (artifacts / "function.zip").write_bytes(b"original")
    (artifacts / "build.json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(b"original").hexdigest(),
                "source_sha256": "old-source",
            }
        )
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "source_hash", lambda: "changed-source")
    manager = Deployment(config, client=Mock())
    with pytest.raises(ValueError, match="changed since"):
        manager.manifest()
    (artifacts / "function.zip").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        manager.manifest()


def test_wait_handles_disappearing_deleted_stack(config):
    error = RuntimeError("gone")
    error.code = "StackNotFound"
    manager = Deployment(config, client=Mock())
    manager.get = Mock(side_effect=error)
    assert manager.wait("deleted", deleting=True)["Status"] == "DELETE_COMPLETE"
    with pytest.raises(RuntimeError):
        manager.wait("missing")
