"""Offline contract checks. No provider requests or end-to-end tests."""

import json

import pytest
from fastapi.testclient import TestClient

from bilibili_transcript.api_parser import DashScopeParser, parse_result
from bilibili_transcript.web.app import create_app
from bilibili_transcript.web.models import Settings
from bilibili_transcript.web.output import export_transcript
from bilibili_transcript.web.store import Store
from dataclasses import asdict


@pytest.mark.parametrize(
    "model,input_key,language_key",
    [
        ("qwen3-asr-flash-filetrans", "file_url", "language"),
        ("qwen-audio-3.1-asr-flash-filetrans", "file_urls", "language_hints"),
        ("paraformer-v2", "file_urls", "language_hints"),
    ],
)
def test_provider_request_shapes(model, input_key, language_key):
    parser = DashScopeParser(model, "test-key")
    body = parser._body("oss://audio/example.mp3")
    assert body["input"][input_key] == (
        "oss://audio/example.mp3" if input_key == "file_url" else ["oss://audio/example.mp3"]
    )
    assert body["parameters"][language_key] == ("zh" if language_key == "language" else ["zh"])
    assert body["parameters"]["channel_id"] == [0]
    parser.language = "auto"
    assert language_key not in parser._body("oss://audio/example.mp3")["parameters"]


def test_cloud_segments_work_with_all_exports(tmp_path):
    document = {
        "transcripts": [
            {
                "sentences": [
                    {
                        "begin_time": 1234,
                        "end_time": 4567,
                        "text": "你好，世界。",
                        "language": "zh",
                    },
                    {"begin_time": 9000, "end_time": 10250, "text": "第二句。", "language": "zh"},
                ]
            }
        ]
    }
    transcript = parse_result(document, "直播", "auto")
    assert transcript.language == "zh"
    assert transcript.segments[0].start == 1.234
    assert transcript.segments[1].end == 10.25
    paths = export_transcript(
        asdict(transcript),
        tmp_path,
        "sample",
        {
            "formats": ["txt", "vtt", "srt", "json"],
            "timestamps": False,
        },
    )
    from pathlib import Path

    contents = [Path(path).read_text(encoding="utf-8") for path in paths]
    assert all("你好，世界。" in content for content in contents)
    assert "00:00:01.234 --> 00:00:04.567" in contents[1]
    assert "00:00:01,234 --> 00:00:04,567" in contents[2]


@pytest.mark.parametrize(
    "sentence",
    [
        {"text": "缺少时间戳"},
        {"text": "错误", "begin_time": 2000, "end_time": 1000},
        {"text": "错误", "begin_time": float("nan"), "end_time": 1000},
    ],
)
def test_invalid_timestamps_fail_instead_of_exporting_broken_subtitles(sentence):
    with pytest.raises(RuntimeError, match="timestamps"):
        parse_result({"transcripts": [{"sentences": [sentence]}]}, "sample")


def test_old_settings_gain_whisper_default(tmp_path):
    store = Store(tmp_path)
    store.execute(
        "UPDATE settings SET value=? WHERE id=1",
        (json.dumps({"parser": {"model": "small", "language": "zh"}}),),
    )
    assert store.settings()["parser"]["provider"] == "whisper"
    assert store.settings()["parser"]["model"] == "small"


def test_keys_are_write_only_and_region_scoped(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_INTL_API_KEY", raising=False)
    app = create_app(tmp_path, start_worker=False)
    store = app.state.store
    with TestClient(app) as client:
        settings = client.get("/api/settings").json()
        settings["parser"].update(provider="asr_flash", api_key="test-secret-key")
        response = client.put("/api/settings", json=settings)
        assert response.status_code == 200
        assert "test-secret-key" not in response.text
        assert store.api_key("beijing") == "test-secret-key"
        assert store.api_key("singapore") == ""
        assert response.json()["parser"]["configured_regions"] == ["beijing"]
        settings = response.json()
        settings["parser"]["api_key"] = ""
        assert client.put("/api/settings", json=settings).status_code == 200
        assert store.api_key("beijing") == "test-secret-key"
        store.import_urls([{"url": "https://www.bilibili.com/video/BV1xx411c7mD"}])
        job = store.create_job("parse", [1], store.settings())
        assert "test-secret-key" not in client.get(f"/api/jobs/{job}").text
        assert "test-secret-key" not in store.one("SELECT value FROM settings WHERE id=1")["value"]
        settings["parser"].update(
            api_key="test-secret-key", region="singapore", provider="paraformer"
        )
        response = client.put("/api/settings", json=settings)
        assert response.status_code == 422
        assert "test-secret-key" not in response.text
        settings["parser"].update(region="beijing", clear_api_key=True, api_key="")
        assert client.put("/api/settings", json=settings).status_code == 200
        assert store.api_key("beijing") == ""


def test_missing_key_is_actionable_and_settings_never_serialize_it():
    with pytest.raises(ValueError, match="API key"):
        DashScopeParser("paraformer-v2", "")
    settings = Settings.model_validate({"parser": {"api_key": "test-secret-key"}})
    assert "test-secret-key" not in settings.model_dump_json()
    assert "api_key" not in settings.model_dump()["parser"]
