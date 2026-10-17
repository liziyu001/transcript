"""Deployment diagnostics must be useful without exposing credentials."""

import importlib.util
from pathlib import Path

import transcript_cloud.storage as storage

spec = importlib.util.spec_from_file_location(
    "deploy_script", Path(__file__).resolve().parents[1] / "scripts" / "deploy.py"
)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_sdk_diagnostic_redacts_environment_and_resolved_credentials(monkeypatch):
    monkeypatch.setenv("TC_DASHSCOPE_KEY", "model-secret")
    monkeypatch.setattr(storage, "credentials", lambda: ("access-id", "access-secret", "sts-token"))
    error = RuntimeError()
    error.code = "InvalidParameter"
    error.message = "bad input access-id access-secret sts-token model-secret"
    error.data = {"RequestId": "request-123"}
    message = deploy.failure_message(error)
    assert "InvalidParameter" in message and "request-123" in message
    for secret in ("access-id", "access-secret", "sts-token", "model-secret"):
        assert secret not in message


def test_sdk_diagnostic_omits_embedded_template(monkeypatch):
    monkeypatch.setattr(storage, "credentials", lambda: (None, None, None))
    message = deploy.failure_message(RuntimeError('TemplateBody: {"secret": "unknown-value"}'))
    assert "unknown-value" not in message
