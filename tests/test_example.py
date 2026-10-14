"""
Example tests showing how to use the API programmatically.
These are NOT integration tests (they don't require actual video URLs).
"""

import tempfile
from pathlib import Path
from bilibili_transcript.parser import Transcript, TranscriptSegment


def test_transcript_formatting():
    """Test different output formats."""

    # Create sample transcript
    transcript = Transcript(
        title="test_video",
        url="https://bilibili.com/example",
        language="zh",
        segments=[
            TranscriptSegment(start=0.0, end=2.5, text="你好，这是测试。"),
            TranscriptSegment(start=2.5, end=5.0, text="这是第二句话。"),
            TranscriptSegment(start=5.0, end=7.5, text="最后一句话。"),
        ],
    )

    # Test plain text format
    plain = transcript.to_text()
    assert "你好，这是测试。" in plain
    assert "这是第二句话。" in plain
    lines = plain.strip().split("\n")
    assert len(lines) == 3

    # Test timestamped format
    ts_text = transcript.to_text_with_timestamps()
    assert "[00:00:00 --> 00:00:02]" in ts_text
    assert "你好，这是测试。" in ts_text

    # Test VTT format
    vtt = transcript.to_vtt()
    assert "WEBVTT" in vtt
    assert "00:00:00.000 --> 00:00:02.500" in vtt


def test_transcript_time_formatting():
    """Test time formatting edge cases."""

    seg = TranscriptSegment(
        start=3661.5,  # 1 hour, 1 minute, 1.5 seconds
        end=3662.0,
        text="test",
    )

    assert str(seg).startswith("[01:01:01 -->")

    # VTT format with milliseconds
    transcript = Transcript("test", "", "zh", [seg])
    vtt_time = transcript._format_vtt_time(3661.5)
    assert "01:01:01.500" in vtt_time


if __name__ == "__main__":
    test_transcript_formatting()
    test_transcript_time_formatting()
    print("OK - Example tests passed")
