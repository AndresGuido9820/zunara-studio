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


def test_fetch_child_progress_is_sent_to_stderr(tmp_path, monkeypatch, capfd):
    fake = tmp_path / "fake-ffmpeg"
    fake.write_text("#!/bin/sh\nprintf 'native child progress\\n'\n")
    fake.chmod(0o700)
    monkeypatch.setattr(fetch_vod, "executable", lambda _: str(fake))
    fetch_vod.ffmpeg_audio("https://example.test/audio", tmp_path / "unused.wav")
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == "native child progress\n"
