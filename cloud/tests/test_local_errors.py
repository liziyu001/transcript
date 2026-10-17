from unittest.mock import patch

import httpx
import pytest

from transcript_cloud.local_client import CloudClient, CloudError


@pytest.mark.parametrize("status", [403, 502])
def test_http_error_includes_method_status_and_request_id_without_body(status):
    response = httpx.Response(
        status,
        text="secret response https://oss.example/signed?token=secret",
        headers={"x-fc-request-id": "trace-123"},
    )
    with patch("transcript_cloud.local_client.httpx.Client") as client:
        client.return_value.__enter__.return_value.request.return_value = response
        with pytest.raises(CloudError) as error:
            CloudClient("https://api.example", "x" * 32).request(
                "PATCH", "/studio/resources/abc", {}
            )
    message = str(error.value)
    assert "PATCH /api/studio/resources/abc" in message
    assert f"HTTP {status}" in message and "trace-123" in message
    assert "secret" not in message


def test_transport_error_preserves_safe_diagnostics():
    with patch("transcript_cloud.local_client.httpx.Client") as client:
        client.return_value.__enter__.return_value.request.side_effect = httpx.ReadTimeout(
            "secret URL"
        )
        with pytest.raises(CloudError) as error:
            CloudClient("https://api.example", "x" * 32).request("POST", "/audio", {})
    assert "ReadTimeout" in str(error.value) and "POST /api/audio" in str(error.value)
    assert "secret" not in str(error.value)


def test_main_logs_cloud_error():
    import sys
    from types import SimpleNamespace

    from transcript_cloud.local_stage import main

    store = SimpleNamespace(job=lambda _: {"items": [{"id": "item"}]})
    with (
        patch.object(sys, "argv", ["stage", "data", "job", "item"]),
        patch("transcript_cloud.local_stage.LocalStore", return_value=store),
        patch("transcript_cloud.local_stage.CloudClient"),
        patch(
            "transcript_cloud.local_stage.execute",
            side_effect=CloudError("PATCH /api/studio/resources/abc HTTP 403"),
        ),
        patch("builtins.print") as output,
    ):
        assert main() == 1
    assert any("HTTP 403" in str(call) for call in output.call_args_list)
