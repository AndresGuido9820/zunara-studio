"""Untrusted download metadata must not make FFmpeg read local paths."""

import pytest

from zunara import fetch_vod


@pytest.mark.parametrize(
    "url",
    [
        "/etc/private",
        "file:///etc/private",
        "http://example.test/a",
        "concat:a|b",
        "https:///no-host",
    ],
)
def test_unsafe_remote_source_never_invokes_ffmpeg(tmp_path, monkeypatch, url):
    def unexpected(*args, **kwargs):
        pytest.fail("FFmpeg must not be called for an unsafe source")

    monkeypatch.setattr(fetch_vod.subprocess, "run", unexpected)
    with pytest.raises(ValueError, match="HTTPS"):
        fetch_vod.ffmpeg_audio(url, tmp_path / "a.wav")
    assert not list(tmp_path.iterdir())
